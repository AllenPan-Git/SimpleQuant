"""
AI 因子挖掘：AI 提出一批因子 → 软件只在挖掘期评估 → 结果发回给 AI → 再提，共若干轮；结束后只对入选因子看一次检验期

防过拟合：
- 区间按时间切成挖掘期和检验期（检验期默认最后 30%）。挖掘期的 IC 样本只取标签（T+1+h 日开盘价）也落在挖掘期内的日子；
  发给 AI 的、决定是否入选的，全部只用挖掘期
- 记下一共试了多少个因子，给出多重检验下的 t 值门槛（Bonferroni），检验期里与现有因子、简单合成并排比较
AI 写的代码在本机自动运行（不像「AI 写因子」那样先给用户看）：先做严格的静态检查（不许 import、读写文件、eval 等），
再在截到较早日期的面板上重算一遍，截止日前几天的值和用完整面板算的不同 = 用到了未来数据，不入选
"""

import dataclasses
from dataclasses import dataclass, field
from statistics import NormalDist

import numpy as np
import pandas as pd

from ..i18n import pick
from . import custom_factors
from .factors import FACTORS, compute, factors_for, preprocess, rank_corr
from .research import forward_returns, rank_ic, sample_dates, summarize_ic

MIN_COVER = 0.5          # 因子至少覆盖挖掘期可选股票的这个比例
CHECK_ROWS = 5           # 防前视：比较截止日前这么多天的值
TOP_EXISTING = 3         # 基准：挖掘期最好的几个现有因子
ALPHA = 0.05
STATUS = ("accepted", "weak", "duplicate", "low_cover", "lookahead", "error", "review", "repeat")


@dataclass
class MineConfig:
    rounds: int = 5              # 轮数 = 调用模型的次数
    per_round: int = 4           # 每轮要几个因子
    horizon: int = 20            # 持有天数（IC 的未来收益期）
    test_frac: float = 0.3       # 检验期占区间的比例
    min_days: int = 250          # 上市满多少天才参与
    min_t: float = 2.0           # 挖掘期 |t| 至少这么大才入选
    max_corr: float = 0.7        # 和现有 / 已入选因子的相关系数绝对值不能超过这个数
    idea: str = ""               # 用户给的方向（可空）


@dataclass
class Candidate:
    round: int
    name: str
    idea: str
    code: str
    desc: str = ""
    status: str = ""             # STATUS 之一
    reason: str = ""             # 出错 / 检查不通过的原因
    ic: float = np.nan           # 挖掘期 IC 均值（原始符号）
    t: float = np.nan
    icir: float = np.nan         # 年化
    pos: float = np.nan          # IC > 0 的比例
    cover: float = np.nan
    corr: float = np.nan         # 和现有 / 已入选因子相关系数绝对值的最大值
    corr_with: str = ""
    test: dict = field(default_factory=dict)    # 检验期：ic（已乘方向）、t、n

    @property
    def direction(self) -> int:
        return -1 if self.ic < 0 else 1

    @property
    def held(self) -> bool:
        """检验期保持住了：按挖掘期方向的 IC 为正，且不低于挖掘期的一半"""
        ic = self.test.get("ic", np.nan)
        return bool(np.isfinite(ic) and ic > 0 and ic >= abs(self.ic) / 2)


def t_needed(tried: int, alpha: float = ALPHA) -> float:
    """试了 tried 个因子时，挑出的最好结果要达到的 |t|（Bonferroni，双侧）"""
    return float(NormalDist().inv_cdf(1 - alpha / (2 * max(tried, 1))))


def cut_panel(panel, end):
    """只保留 end 及以前的数据（检查因子有没有用到未来）"""
    return dataclasses.replace(panel, fields={k: v.loc[:end] for k, v in panel.fields.items()},
                               member=panel.member.loc[:end])


def _same(a: pd.DataFrame, b: pd.DataFrame) -> bool:
    a, b = a.to_numpy(dtype=float), b.to_numpy(dtype=float)
    return bool(np.allclose(a, b, rtol=1e-6, atol=1e-9, equal_nan=True))


class Miner:
    """
    一次挖掘。用法：m = Miner(panel, start, cfg); m.baseline(); 然后每轮 m.message() → 模型 → m.add_round(...)；
    最后 m.finish() 才计算检验期。run() 把这些串起来
    """

    def __init__(self, panel, start, cfg: MineConfig, lang: str = "zh"):
        self.panel, self.cfg, self.lang = panel, cfg, lang
        h = cfg.horizon
        cal = panel.calendar[panel.calendar >= pd.Timestamp(start)]
        split = int(len(cal) * (1 - cfg.test_frac))
        if split < 3 * (h + 1) or len(cal) - split < 3 * (h + 1):
            raise ValueError("区间过短，挖掘期或检验期不足 3 期 / Range too short: fewer than 3 periods to mine or test")
        self.start, self.mine_end, self.test_start, self.end = cal[0], cal[split - 1], cal[split], cal[-1]
        self.mine_dates = sample_dates(cal[:split], h)
        self.test_dates = sample_dates(cal[split:], h)
        self.cuts = [cal[split // 2], cal[split - 1]]      # 防前视检查的截止日，都在挖掘期内
        self.mask = panel.eligible(True, cfg.min_days)
        self.fwd = forward_returns(panel, h)
        self.ranks: dict[str, pd.DataFrame] = {}           # 名称 → 挖掘期采样日的排名（现有因子 + 已入选因子）
        self.z: dict[str, pd.DataFrame] = {}               # 名称 → 标准化因子（算检验期和合成用）
        self.existing: list[dict] = []                     # 现有因子在挖掘期的表现
        self.candidates: list[Candidate] = []
        self.notes: list[str] = []                         # 模型每轮的说明
        self.calls = 0
        self.wait_limit = None                             # 等模型回复的上限（秒）；None = llm.providers.total_wait
        self.result: dict | None = None

    # ---------- 评估 ----------
    def _stats(self, z: pd.DataFrame, dates) -> tuple:
        ic = rank_ic(z.loc[dates], self.fwd.loc[dates])
        return ic, summarize_ic(ic, self.cfg.horizon)

    def usable_keys(self) -> list[str]:
        p = self.panel
        return [k for k in factors_for(p.kind)
                if (p.has_fin or not FACTORS[k].get("requires_fin")) and (p.div_codes or not FACTORS[k].get("requires_div"))]

    def baseline(self, progress=None) -> None:
        """现有因子（内置 + 已保存的自定义因子）在挖掘期的表现：发给 AI 参考，也用来判断新因子是否重复"""
        keys = self.usable_keys()
        for i, k in enumerate(keys, 1):
            try:
                z = preprocess(compute(self.panel, k), self.mask)
            except Exception:  # noqa: BLE001 - 已保存的自定义因子出错时跳过
                continue
            _, s = self._stats(z, self.mine_dates)
            name = pick(FACTORS[k]["label"], self.lang)
            self.existing.append({"key": k, "name": name, "ic": s.mean, "t": s.t_stat, "n": s.n})
            self.ranks[name] = z.loc[self.mine_dates].rank(axis=1)
            self.z[name] = z
            if progress:
                progress(i, len(keys))

    def evaluate(self, c: Candidate) -> Candidate:
        """检查并在挖掘期评估一个候选因子，填好 status 等；入选的登记进相关性比较"""
        if not c.code.strip():
            c.status, c.reason = "error", _t("没有代码", "no code", self.lang)
            return c
        if any(o.code.strip() == c.code.strip() for o in self.candidates if o is not c):
            c.status = "repeat"
            return c
        issues = custom_factors.check_syntax(c.code, self.lang) or custom_factors.review(c.code, self.lang, strict=True)
        if issues:
            c.status, c.reason = "review", issues[0]
            return c
        try:
            raw = custom_factors.evaluate(c.code, self.panel, self.lang)
            for cut in self.cuts:
                part = custom_factors.evaluate(c.code, cut_panel(self.panel, cut), self.lang)
                if not _same(part.iloc[-CHECK_ROWS:], raw.loc[:cut].iloc[-CHECK_ROWS:]):
                    c.status = "lookahead"
                    c.reason = _t(f"截至 {cut.date()} 重新计算时，最后几日的值与使用完整数据计算的结果不同",
                                  f"values up to {cut.date()} change when later data is removed", self.lang)
                    return c
        except custom_factors.FactorCodeError as e:
            c.status, c.reason = "error", str(e)
            return c
        except Exception as e:  # noqa: BLE001
            c.status, c.reason = "error", f"{type(e).__name__}: {e}"
            return c
        z = preprocess(raw.replace([np.inf, -np.inf], np.nan), self.mask)
        m = self.mask.loc[self.mine_dates]
        c.cover = float(z.loc[self.mine_dates].notna().sum().sum() / max(int(m.sum().sum()), 1))
        if c.cover < MIN_COVER:
            c.status = "low_cover"
            c.reason = _t(f"挖掘期内有值的仅占可选股票的 {c.cover:.0%}，低于 {MIN_COVER:.0%}（多为空值，请检查 min_periods 与条件筛选）",
                          f"only {c.cover:.0%} of eligible stock-days have a value, below {MIN_COVER:.0%} "
                          f"(mostly NaN; check min_periods and conditional filters)", self.lang)
            return c
        _, s = self._stats(z, self.mine_dates)
        c.ic, c.t, c.icir, c.pos = s.mean, s.t_stat, s.ir_annual, s.positive
        rank = z.loc[self.mine_dates].rank(axis=1)
        corrs = {n: rank_corr(rank, r) for n, r in self.ranks.items()}
        corrs = {n: v for n, v in corrs.items() if np.isfinite(v)}
        if corrs:
            c.corr_with = max(corrs, key=lambda n: abs(corrs[n]))
            c.corr = abs(corrs[c.corr_with])
        if not np.isfinite(c.t):
            c.status = "low_cover"
            c.reason = _t("有效的 IC 期数过少，无法计算 t 值（多数调仓日的值为空或完全相同）",
                          "too few valid IC periods to compute t (values missing or constant on most rebalance dates)",
                          self.lang)
        elif np.isfinite(c.corr) and c.corr >= self.cfg.max_corr:
            c.status = "duplicate"
        elif abs(c.t) < self.cfg.min_t:
            c.status = "weak"
        else:
            c.name = self._unique(c.name)
            c.status = "accepted"
            self.ranks[c.name] = rank
            self.z[c.name] = z
        return c

    def _unique(self, name: str) -> str:
        name = name or _t("AI 因子", "AI factor", self.lang)
        taken = set(self.ranks) | {c.name for c in self.accepted}
        out, i = name, 2
        while out in taken:
            out, i = f"{name} {i}", i + 1
        return out

    @property
    def accepted(self) -> list[Candidate]:
        return [c for c in self.candidates if c.status == "accepted"]

    @property
    def tried(self) -> int:
        """计入多重检验的个数：真正算出 IC 的因子"""
        return sum(1 for c in self.candidates if np.isfinite(c.t))

    # ---------- 和模型的对话 ----------
    def message(self) -> str:
        """这一轮发给模型的内容：只有挖掘期的结果"""
        cfg = self.cfg
        what = "convertible bonds" if self.panel.kind == "cb" else "stocks"
        lines = [f"Universe: {len(self.panel.codes)} {what}. Mining period: {self.start.date()} to {self.mine_end.date()} "
                 f"({len(self.mine_dates)} IC samples). Horizon h = {cfg.horizon} trading days.",
                 f"Acceptance: |t| >= {cfg.min_t:g}, coverage >= {MIN_COVER:.0%}, |correlation| < {cfg.max_corr:g} "
                 f"with every existing or accepted factor.",
                 "", "Existing factors (already available to the user; new factors must differ from them):",
                 "name | IC | t"]
        lines += [f"{e['name']} | {_n(e['ic'], 3)} | {_n(e['t'])}" for e in self.existing if np.isfinite(e["t"])]
        if self.candidates:
            lines += ["", "Your earlier proposals and their results (mining period only):",
                      "round | name | idea | IC | t | max |corr| (with) | result"]
            for c in self.candidates:
                res = c.status + (f": {c.reason[:160]}" if c.reason else "")
                corr = f"{_n(c.corr)} ({c.corr_with})" if c.corr_with else "-"
                lines.append(f"{c.round} | {c.name} | {c.idea} | {_n(c.ic, 3)} | {_n(c.t)} | {corr} | {res}")
        if cfg.idea.strip():
            lines += ["", "The user asks you to focus on: " + cfg.idea.strip()]
        lines += ["", f"Propose {cfg.per_round} new factors."]
        return "\n".join(lines)

    def add_round(self, reply: dict, progress=None) -> list[Candidate]:
        r = 1 + max((c.round for c in self.candidates), default=0)
        if reply.get("notes"):
            self.notes.append(reply["notes"])
        new = []
        for f in (reply.get("factors") or [])[: self.cfg.per_round]:
            c = Candidate(round=r, name=f.get("name", ""), idea=f.get("idea", ""), code=f.get("code", ""),
                          desc=f.get("desc", ""))
            self.candidates.append(c)
            self.evaluate(c)
            new.append(c)
            if progress:
                progress(c)
        return new

    # ---------- 检验期 ----------
    def _test(self, z: pd.DataFrame, direction: int) -> dict:
        _, s = self._stats(z, self.test_dates)
        return {"ic": s.mean * direction if np.isfinite(s.mean) else np.nan,
                "t": s.t_stat * direction if np.isfinite(s.t_stat) else np.nan, "n": s.n}

    def _composite(self, items: list[tuple[str, int]]) -> pd.DataFrame | None:
        """等权合成：Σ 方向 × 标准化因子（任一因子缺失的股票不参与）"""
        if not items:
            return None
        total = sum(self.z[n] * d for n, d in items)
        return total / len(items)

    def finish(self) -> dict:
        """挖掘结束后只算一次检验期：入选因子、挖掘期最好的几个现有因子、两者的等权合成"""
        for c in self.accepted:
            c.test = self._test(self.z[c.name], c.direction)
        best = sorted((e for e in self.existing if np.isfinite(e["t"])), key=lambda e: -abs(e["t"]))[:TOP_EXISTING]
        bench = []
        for e in best:
            d = -1 if e["ic"] < 0 else 1
            bench.append({"name": e["name"], "mine_ic": e["ic"] * d, "mine_t": e["t"] * d, **self._test(self.z[e["name"]], d)})
        combos = {}
        for key, items in (("mined", [(c.name, c.direction) for c in self.accepted]),
                           ("existing", [(e["name"], -1 if e["ic"] < 0 else 1) for e in best])):
            z = self._composite(items)
            if z is not None:
                _, s = self._stats(z, self.mine_dates)
                combos[key] = {"factors": len(items), "mine_ic": s.mean, "mine_t": s.t_stat, **self._test(z, 1)}
        self.result = {"tried": self.tried, "proposed": len(self.candidates), "accepted": len(self.accepted),
                       "calls": self.calls, "t_needed": t_needed(self.tried), "min_t": self.cfg.min_t,
                       "n_test": len(self.test_dates), "bench": bench, "combos": combos,
                       "held": sum(1 for c in self.accepted if c.held),
                       "lucky": sum(1 for c in self.accepted if abs(c.t) < t_needed(self.tried))}
        return self.result

    # ---------- 整个流程 ----------
    def run(self, provider, log=None, progress=None, stop=None) -> dict:
        """
        log(str)：一行日志；progress(比例, 文字)；stop()：为真时停下，等模型回复时也立刻停（已有的结果照常算检验期）
        模型出错（网络、格式）或 wait_limit 秒没有回复时停止挖掘，错误写进日志，已有结果照常收尾
        """
        from ..llm import propose_factors, LLMTimeout, Stopped
        log = log or (lambda s: None)
        progress = progress or (lambda v, s="": None)
        say = lambda zh, en: log(_t(zh, en, self.lang))
        if not self.existing:
            say("计算现有因子在挖掘期的表现…", "Measuring existing factors on the mining period…")
            self.baseline(lambda i, n: progress(0.1 * i / n, f"{i}/{n}"))
        rounds = self.cfg.rounds
        for r in range(1, rounds + 1):
            if stop and stop():
                say("已停止", "Stopped")
                break
            say(f"第 {r}/{rounds} 轮：请模型提出 {self.cfg.per_round} 个因子…",
                f"Round {r}/{rounds}: asking the model for {self.cfg.per_round} factors…")
            frac = 0.1 + 0.9 * (r - 1) / rounds
            progress(frac, _t(f"第 {r}/{rounds} 轮", f"Round {r}/{rounds}", self.lang))
            try:
                self.calls += 1
                tick = lambda s: progress(frac, _t(f"第 {r}/{rounds} 轮：等待模型回复 {s} 秒",
                                                   f"Round {r}/{rounds}: waiting for the model, {s}s", self.lang))
                reply = propose_factors(provider, self.message(), self.lang, self.panel.kind, stop=stop, tick=tick,
                                        limit=self.wait_limit)
            except Stopped:
                say("已停止（这一轮的回复不再等待）", "Stopped (no longer waiting for this round's reply)")
                break
            except LLMTimeout:
                say("模型长时间没有回复，挖掘停止。服务繁忙时可稍后再试，或减少每轮因子数",
                    "No reply from the model for a long time, mining stopped. If the service is busy, "
                    "try again later or ask for fewer factors per round")
                break
            except Exception as e:  # noqa: BLE001
                say(f"调用模型出错，挖掘停止：{type(e).__name__}: {e}",
                    f"Model call failed, mining stopped: {type(e).__name__}: {e}")
                break
            if reply["notes"]:
                log("  " + reply["notes"])
            self.add_round(reply, lambda c: log("  " + line(c, self.lang)))
            progress(0.1 + 0.9 * r / rounds, _t(f"第 {r}/{rounds} 轮", f"Round {r}/{rounds}", self.lang))
        say("计算检验期…", "Evaluating the test period…")
        return self.finish()


# ---------- 文字 ----------
def _t(zh: str, en: str, lang: str) -> str:
    return zh if lang == "zh" else en


def _n(v, digits: int = 2) -> str:
    return f"{v:.{digits}f}" if isinstance(v, (int, float)) and np.isfinite(v) else "-"


STATUS_TEXT = {
    "accepted": ("入选", "accepted"), "weak": ("不显著", "not significant"), "duplicate": ("与已有因子重复", "duplicate"),
    "low_cover": ("覆盖不足", "low coverage"), "lookahead": ("使用了未来数据", "uses future data"),
    "error": ("运行出错", "error"), "review": ("未通过检查", "rejected by check"), "repeat": ("代码重复", "repeated code"),
}


def status_text(status: str, lang: str) -> str:
    zh, en = STATUS_TEXT.get(status, (status, status))
    return _t(zh, en, lang)


def line(c: Candidate, lang: str) -> str:
    """日志里的一行"""
    s = f"{c.name or '-'}：{status_text(c.status, lang)}" if lang == "zh" else f"{c.name or '-'}: {status_text(c.status, lang)}"
    if np.isfinite(c.t):
        s += f"  IC {_n(c.ic, 3)}  t {_n(c.t)}"
    if c.corr_with and np.isfinite(c.corr):
        s += _t(f"  与「{c.corr_with}」相关 {_n(c.corr)}", f"  corr {_n(c.corr)} with {c.corr_with}", lang)
    if c.reason:
        s += "  · " + c.reason.splitlines()[0][:120]
    return s


def passes(c: Candidate, result: dict) -> bool:
    """挖掘期 |t| 达到多重检验门槛"""
    return bool(np.isfinite(c.t) and abs(c.t) >= result["t_needed"])


def _md_table(head: list[str], rows: list[list]) -> list[str]:
    cell = lambda v: str(v).replace("|", "\\|").replace("\n", " ")
    return ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)] + \
        ["| " + " | ".join(cell(v) for v in r) + " |" for r in rows]


def to_markdown(m: "Miner", lang: str = "zh", title: str = "", findings: list[str] = ()) -> str:
    """挖掘记录（导出用）：设置、两段区间、可信度检查、入选因子、与现有因子比较、全部候选及代码、模型每轮的说明"""
    zh = lang == "zh"
    T = lambda a, b: a if zh else b
    r, cfg = m.result or {}, m.cfg
    yes, no = T("是", "yes"), T("否", "no")
    P = lambda x: f"（{x}）" if zh else f" ({x})"
    out = [f"# {T('AI 因子挖掘记录', 'AI factor mining record')}" + (f" · {title}" if title else ""), "",
           f"- {T('股票数', 'Securities')}: {len(m.panel.codes)}",
           f"- {T('挖掘期', 'Mining period')}: {m.start.date()} ~ {m.mine_end.date()}" + P(f"{len(m.mine_dates)} {T('期', 'periods')}"),
           f"- {T('检验期', 'Test period')}: {m.test_start.date()} ~ {m.end.date()}" + P(f"{len(m.test_dates)} {T('期', 'periods')}"),
           f"- {T('设置', 'Settings')}: {T('轮数', 'rounds')} {cfg.rounds} · {T('每轮', 'per round')} {cfg.per_round} · "
           f"{T('持有天数', 'horizon')} {cfg.horizon} · {T('检验期比例', 'test share')} {cfg.test_frac:.0%} · "
           f"|t| ≥ {cfg.min_t:g} · {T('相关系数', 'correlation')} < {cfg.max_corr:g} · "
           f"{T('上市天数', 'listed days')} ≥ {cfg.min_days}",
           f"- {T('挖掘方向', 'Direction')}: {cfg.idea.strip() or '—'}"]
    if r:
        out += [f"- {T('提出', 'Proposed')} {r['proposed']} · {T('得出结果', 'evaluated')} {r['tried']} · "
                f"{T('入选', 'accepted')} {r['accepted']} · {T('调用模型', 'model calls')} {r['calls']} · "
                f"{T('多重检验门槛', 'multiple-testing threshold')} |t| ≥ {_n(r['t_needed'])}"]
    if findings:
        out += ["", f"## {T('可信度检查', 'Credibility')}", ""] + [f"- {f}" for f in findings]
    if r and m.accepted:
        out += ["", f"## {T('入选因子', 'Accepted factors')}", "",
                T("IC 已按方向调整：正数表示按该方向选股有效。", "IC is adjusted for direction: positive means it worked."), ""]
        out += _md_table([T("因子", "Factor"), T("方向", "Direction"), T("挖掘期 IC", "Mining IC"), T("挖掘期 t", "Mining t"),
                          T("达到门槛", "Passes threshold"), T("检验期 IC", "Test IC"), T("检验期 t", "Test t"),
                          T("通过检验期", "Held up"), T("最相关的已有因子", "Most correlated")],
                         [[c.name, T("越大越好", "higher") if c.direction > 0 else T("越小越好", "lower"),
                           _n(abs(c.ic), 3), _n(abs(c.t)), yes if passes(c, r) else no, _n(c.test.get("ic"), 3),
                           _n(c.test.get("t")), yes if c.held else no, f"{c.corr_with} {_n(c.corr)}" if c.corr_with else "—"]
                          for c in m.accepted])
        rows = [[b["name"], _n(b["mine_ic"], 3), _n(b["mine_t"]), _n(b["ic"], 3), _n(b["t"])] for b in r["bench"]]
        for key, zh_l, en_l in (("mined", "入选因子等权合成", "Accepted, equal-weight"),
                                ("existing", "现有最好因子等权合成", "Best existing, equal-weight")):
            cmb = r["combos"].get(key)
            if cmb:
                rows.append([T(zh_l, en_l) + P(cmb['factors']), _n(cmb["mine_ic"], 3), _n(cmb["mine_t"]),
                             _n(cmb["ic"], 3), _n(cmb["t"])])
        out += ["", f"## {T('与现有因子比较', 'Compared with existing factors')}", ""]
        out += _md_table([T("因子", "Factor"), T("挖掘期 IC", "Mining IC"), T("挖掘期 t", "Mining t"),
                          T("检验期 IC", "Test IC"), T("检验期 t", "Test t")], rows)
    out += ["", f"## {T('全部候选', 'All candidates')}" + P(len(m.candidates)), ""]
    out += _md_table([T("轮", "Round"), T("因子", "Factor"), T("结果", "Result"), T("挖掘期 IC", "Mining IC"),
                      T("挖掘期 t", "Mining t"), T("最相关的已有因子", "Most correlated"), T("思路", "Idea")],
                     [[c.round, c.name, status_text(c.status, lang), _n(c.ic, 3), _n(c.t),
                       f"{c.corr_with} {_n(c.corr)}" if c.corr_with else "—", c.idea] for c in m.candidates])
    out += ["", f"## {T('代码', 'Code')}"]
    for c in m.candidates:
        out += ["", f"### {T('第', 'Round ')}{c.round}{T(' 轮', '')} · {c.name} · {status_text(c.status, lang)}", ""]
        if c.idea:
            out.append(f"{T('思路', 'Idea')}：{c.idea}" if zh else f"Idea: {c.idea}")
        if c.desc:
            out.append(f"{T('说明', 'Description')}：{c.desc}" if zh else f"Description: {c.desc}")
        if c.reason:
            out += ["", "```text", c.reason.strip(), "```"]
        out += ["", "```python", c.code.rstrip(), "```"]
    if m.notes:
        out += ["", f"## {T('模型每轮的说明', 'Model notes per round')}", ""] + \
            [f"{i}. {n}" for i, n in enumerate(m.notes, 1)]
    return "\n".join(out) + "\n"
