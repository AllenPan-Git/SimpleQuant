"""
回测方案文件（.sqplan）：把回测页的全部设置——策略、标的、区间、资金与费率、组合 / 分别回测——存成一个 JSON 文件，
在另一台电脑上导入即可还原并重新回测，用于分享研究、复核结果、迁移设置。

- 只包含策略模板与条件组件（纯数据）；代码策略不能放进方案文件：导入别人的代码等于在本机执行它
- 标的只记录如何获取数据（数据源、代码、频率、复权），不包含行情本身；导入时本机没有的数据可一键下载
- 可附带导出时的回测结果，导入后重新回测时用于核对；行情来源不同（如东方财富与新浪）或数据修订会导致结果不一致

多因子选股方案（version 2，strategy.kind = "selection"）：股票池、因子与权重、选股规则、区间、资金与费率。
用到的自定义因子连同代码一起写进文件（导入前显示代码，由用户确认后才保存、运行）；
另记因子预热的起始日（warmup_start），本机数据覆盖到这天才能得到相同的因子值。
"""

import copy
import datetime as dt
import json
from dataclasses import asdict, fields

from . import __version__
from .engine.runner import BrokerConfig

FORMAT = "simplequant-plan"
VERSION = 2                     # 能读取的最高版本
TIMING_VERSION = 1              # 择时方案仍写 1，旧版软件也能导入
SELECTION_VERSION = 2           # 选股方案：0.2.4 及以前的版本会提示「请先更新软件」
SUFFIX = ".sqplan"
DOWNLOADABLE = ("akshare", "baostock")          # 导入时可以自动下载的数据源
RESULT_KEYS = ("total_return", "benchmark_return", "max_drawdown", "trades")
MODES = ("portfolio", "batch")


class PlanError(ValueError):
    """方案文件无法导入；args[0] 是文本键（ui/texts.py 的 plan.err_*），args[1] 是参数"""

    def __init__(self, key: str, **kw):
        super().__init__(key, kw)
        self.key, self.kw = key, kw


def make_plan(name: str, spec: dict, metas: list, start, end, broker: BrokerConfig, mode: str = "portfolio",
              result: dict | None = None) -> dict:
    """
    :param spec: 策略描述（不含 name）
    :param metas: 所选数据的 DatasetMeta
    :param result: 用同样设置得到的回测指标（可选，导入方回测后用于核对）
    """
    spec = copy.deepcopy({k: v for k, v in spec.items() if k != "name"})
    if spec.get("kind") not in ("template", "rule"):
        raise PlanError("plan.err_code")
    plan = {
        "format": FORMAT, "version": TIMING_VERSION, "app": __version__,
        "created": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "name": name, "strategy": spec,
        "assets": [{"name": m.name, "symbol": m.symbol, "source": m.source, "freq": m.freq, "adjust": m.adjust,
                    "asset": m.extra.get("asset", ""), "provider": m.extra.get("provider", "")} for m in metas],
        "range": [str(start), str(end)],
        "broker": asdict(broker),
        "mode": mode if mode in MODES and len(metas) > 1 else "portfolio",
    }
    if result:
        plan["result"] = {k: result[k] for k in RESULT_KEYS if k in result}
    return plan


def make_selection_plan(name: str, spec: dict, start, end, warmup_start, broker: BrokerConfig,
                        result: dict | None = None) -> dict:
    """
    多因子选股方案
    :param warmup_start: 面板的起始日（回测开始前用于预热因子）
    :param result: 用同样设置得到的回测指标（可选）
    """
    from .stocks import custom_factors as cf
    spec = copy.deepcopy({k: v for k, v in spec.items() if k != "name"})
    if spec.get("kind") != "selection" or not spec.get("factors"):
        raise PlanError("plan.err_format")
    saved = cf.list_factors()
    custom = {}
    for f in spec["factors"]:
        if cf.is_custom(f["key"]):
            d = saved.get(f["key"])
            if d is None:
                raise PlanError("plan.err_factor_missing", name=f["key"])
            custom[f["key"]] = {k: d.get(k, "") for k in ("name", "code", "direction", "desc")}
    plan = {
        "format": FORMAT, "version": SELECTION_VERSION, "app": __version__,
        "created": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "name": name, "strategy": spec,
        "range": [str(start), str(end)], "warmup_start": str(warmup_start),
        "broker": asdict(broker),
        "custom_factors": custom,
    }
    if result:
        plan["result"] = {k: result[k] for k in RESULT_KEYS if k in result}
    return plan


def is_selection(plan: dict) -> bool:
    return plan.get("strategy", {}).get("kind") == "selection"


def dumps(plan: dict) -> str:
    return json.dumps(plan, ensure_ascii=False, indent=2)


def loads(text: str | bytes) -> dict:
    """读取并检查方案文件；有问题时抛出 PlanError"""
    from .rules import validate
    from .strategies import TEMPLATES
    try:
        plan = json.loads(text.decode("utf-8-sig") if isinstance(text, bytes) else text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PlanError("plan.err_format") from None
    if not isinstance(plan, dict) or plan.get("format") != FORMAT:
        raise PlanError("plan.err_format")
    if int(plan.get("version", 0)) > VERSION:
        raise PlanError("plan.err_newer", app=plan.get("app", "?"))

    spec = plan.get("strategy")
    if not isinstance(spec, dict):
        raise PlanError("plan.err_format")
    if spec.get("kind") == "selection":
        _check_selection(plan)
        return _check_common(plan)
    if spec.get("kind") == "code":
        raise PlanError("plan.err_code")
    if spec.get("kind") == "template":
        if spec.get("template") not in TEMPLATES:
            raise PlanError("plan.err_template", name=spec.get("template", "?"))
    elif spec.get("kind") == "rule":
        errors = validate(spec.get("rule") or {})
        if errors:
            raise PlanError("plan.err_rule", detail="；".join(errors[:3]))
    else:
        raise PlanError("plan.err_format")

    assets = plan.get("assets")
    if not isinstance(assets, list) or not assets or not all(isinstance(a, dict) and a.get("symbol") for a in assets):
        raise PlanError("plan.err_format")
    plan = _check_common(plan)
    plan["mode"] = plan.get("mode") if plan.get("mode") in MODES else "portfolio"
    return plan


def _check_selection(plan: dict):
    """选股方案：股票池、因子都要是本版本认识的；自定义因子要带代码且语法正确（这里不运行）"""
    import re
    from .stocks import FACTORS, UNIVERSES, custom_factors as cf
    spec = plan["strategy"]
    if spec.get("universe") not in UNIVERSES:
        raise PlanError("plan.err_universe", name=spec.get("universe", "?"))
    factors = spec.get("factors")
    if not isinstance(factors, list) or not factors or not all(isinstance(f, dict) and f.get("key") for f in factors):
        raise PlanError("plan.err_format")
    custom = plan.get("custom_factors") or {}
    if not isinstance(custom, dict):
        raise PlanError("plan.err_format")
    for f in factors:
        k = f["key"]
        if cf.is_custom(k):
            d = custom.get(k)
            if not re.fullmatch(r"u_[0-9a-f]{8}", k) or not isinstance(d, dict) or not isinstance(d.get("code"), str) \
                    or not str(d.get("name") or "").strip():
                raise PlanError("plan.err_format")
            errors = cf.check_syntax(d["code"])
            if errors:
                raise PlanError("plan.err_factor_code", name=d["name"], detail=errors[0])
        elif k not in FACTORS:
            raise PlanError("plan.err_factor", name=k)
    plan["custom_factors"] = {k: custom[k] for k in (f["key"] for f in factors) if k in custom}
    try:
        int(spec.get("top_n", 0))
    except (TypeError, ValueError):
        raise PlanError("plan.err_format") from None


def _check_common(plan: dict) -> dict:
    """区间、资金与费率、名称"""
    try:
        start, end = (dt.date.fromisoformat(str(d)) for d in plan.get("range", []))
    except (TypeError, ValueError):
        raise PlanError("plan.err_format") from None
    if start >= end:
        raise PlanError("plan.err_format")

    known = {f.name: f.type for f in fields(BrokerConfig)}
    broker = {k: v for k, v in (plan.get("broker") or {}).items() if k in known}
    try:
        BrokerConfig(**broker)
    except TypeError:
        raise PlanError("plan.err_format") from None
    plan["broker"] = broker
    plan["range"] = [start.isoformat(), end.isoformat()]
    try:
        warm = dt.date.fromisoformat(str(plan.get("warmup_start") or start))
    except ValueError:
        warm = start
    plan["warmup_start"] = min(warm, start).isoformat()
    plan["name"] = str(plan.get("name") or "").strip() or "方案"
    return plan


def broker_of(plan: dict) -> BrokerConfig:
    """方案里的资金与费率；文件里没写的项用默认值（涨跌停与停牌规则默认开启）"""
    return BrokerConfig(**plan["broker"])


def match(asset: dict, metas: list, start: str, end: str):
    """本机数据库中能用于该标的的数据：数据源、代码、频率、复权都相同，且覆盖方案的区间；没有则返回 None"""
    cands = [m for m in metas if m.source == asset.get("source") and m.symbol == asset["symbol"]
             and m.freq == asset.get("freq", "1d") and (m.adjust or "") == (asset.get("adjust") or "")
             and m.start <= start and m.end >= end]
    if not cands:
        return None
    same = [m for m in cands if m.extra.get("provider", "") == asset.get("provider", "")]
    return (same or cands)[0]


def check_assets(plan: dict, metas: list) -> list[dict]:
    """
    每个标的的状态：{"asset", "meta"（本机可用的数据或 None）, "status"}
    status：ok 本机已有 / download 可以下载 / manual 本地导入的数据，需要自行导入 CSV
    """
    start, end = plan["range"]
    out = []
    for a in plan["assets"]:
        m = match(a, metas, start, end)
        status = "ok" if m else "download" if a.get("source") in DOWNLOADABLE else "manual"
        out.append({"asset": a, "meta": m, "status": status})
    return out


def provider_differs(asset: dict, meta) -> bool:
    """本机数据的实际行情来源与方案不同（如方案用东方财富、本机数据来自新浪）：复权方式可能不同，结果会有差异"""
    want, got = asset.get("provider", ""), (meta.extra.get("provider", "") if meta else "")
    return bool(want and got and want != got)


def compare(recorded: dict | None, metrics: dict, tol: float = 0.0005) -> str | None:
    """与方案记录的结果核对：None（方案没有记录结果）/ "same" / "differs" """
    if not recorded or "total_return" not in recorded:
        return None
    same = abs(float(recorded["total_return"]) - float(metrics.get("total_return", 0))) <= tol
    if "trades" in recorded:
        same = same and int(recorded["trades"]) == int(metrics.get("trades", -1))
    return "same" if same else "differs"


# ---------------- 选股方案的数据 ----------------
def needs(plan: dict) -> dict:
    """选股方案除行情外还需要的数据：财务（财务因子、市值中性化）、分红（现金分红、分红字段）、行业（行业中性化）"""
    from .stocks import FACTORS, custom_factors as cf, universe as U
    spec = plan["strategy"]
    if U.kind(spec["universe"]) == "cb":
        return {"fin": False, "div": False, "industry": False}
    custom = plan.get("custom_factors") or {}

    def flag(f, name, uses):
        d = custom.get(f["key"])
        return uses(d["code"]) if d else bool(FACTORS.get(f["key"], {}).get(name))
    neutral = spec.get("neutralize") or {}
    return {"fin": any(flag(f, "requires_fin", cf.uses_fin) for f in spec["factors"]) or bool(neutral.get("size")),
            "div": spec.get("dividend") == "cash" or any(flag(f, "requires_div", cf.uses_div) for f in spec["factors"]),
            "industry": bool(neutral.get("industry"))}


def selection_missing(plan: dict, store=None) -> list[str]:
    """
    本机选股数据缺少的部分（空列表 = 可以直接回测）：
    data 没有该股票池的数据 / range 数据没有覆盖方案的区间（含预热）/ fin 财务 / div 分红 / industry 行业分类
    """
    import pandas as pd
    from .stocks import StockStore, universe as U
    uni = plan["strategy"]["universe"]
    if not U.ready(uni, store):
        return ["data"]
    idx = U.load_benchmark(uni, store).index
    missing = []
    # 预热起始日可能不是交易日，留几天余量
    if idx[0] > pd.Timestamp(plan["warmup_start"]) + pd.Timedelta(days=10) or idx[-1] < pd.Timestamp(plan["range"][1]):
        missing.append("range")
    if U.kind(uni) == "stock":
        st = store or StockStore()
        codes = sorted(st.load_universe(uni)["code"].unique())
        n = needs(plan)
        if n["fin"] and not any(st.has_fin(c) for c in codes):
            missing.append("fin")
        if n["div"] and not any(st.has_div(c) for c in codes):
            missing.append("div")
        if n["industry"] and not (st.root / "industry.parquet").exists():
            missing.append("industry")
    return missing


def download_selection(plan: dict, workers: int = 4, progress=None) -> dict[str, str]:
    """
    下载（增量更新）方案需要的选股数据，区间从预热起始日到方案结束日；返回失败的 {代码: 错误}
    progress(步骤, 完成数, 总数)；股票的步骤为 universe / index / stocks / fin / div，可转债见 CBStore.update_all
    """
    from .stocks import StockStore, UNIVERSES, universe as U
    uni = plan["strategy"]["universe"]
    start, end = plan["warmup_start"], plan["range"][1]
    if U.kind(uni) == "cb":
        from .bonds import CBStore
        return CBStore().update_all(start, end, workers=min(workers, 3), progress=progress)

    def step(name):
        if progress:
            progress(name, 0, 1)
            return lambda i, n: progress(name, i, n)
        return None
    st, n = StockStore(), needs(plan)
    u = st.update_universe(uni, start, end, progress=step("universe"))
    step("index")
    st.update_index(UNIVERSES[uni]["index"], start, end)
    st.update_basics()
    codes = sorted(u["code"].unique())
    errors = st.update(codes, start, end, workers=workers, progress=step("stocks"))
    if n["fin"]:
        errors.update(st.update_fundamentals(codes, int(start[:4]) - 1, workers=workers, progress=step("fin")))
    if n["industry"]:
        st.update_industry()
    if n["div"]:
        errors.update(st.update_dividends(codes, int(start[:4]) - 1, workers=workers, progress=step("div")))
    return errors
