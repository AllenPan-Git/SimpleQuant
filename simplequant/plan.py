"""
回测方案文件（.sqplan）：把回测页的全部设置——策略、标的、区间、资金与费率、组合 / 分别回测——存成一个 JSON 文件，
在另一台电脑上导入即可还原并重新回测，用于分享研究、复核结果、迁移设置。

- 只包含策略模板与条件组件（纯数据）；代码策略不能放进方案文件：导入别人的代码等于在本机执行它
- 标的只记录如何获取数据（数据源、代码、频率、复权），不包含行情本身；导入时本机没有的数据可一键下载
- 可附带导出时的回测结果，导入后重新回测时用于核对；行情来源不同（如东方财富与新浪）或数据修订会导致结果不一致
"""

import copy
import datetime as dt
import json
from dataclasses import asdict, fields

from . import __version__
from .engine.runner import BrokerConfig

FORMAT = "simplequant-plan"
VERSION = 1
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
        "format": FORMAT, "version": VERSION, "app": __version__,
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
    plan["mode"] = plan.get("mode") if plan.get("mode") in MODES else "portfolio"
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
