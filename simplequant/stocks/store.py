"""
多因子选股的数据层（BaoStock）

目录：<项目>/data_cache/stocks/
    daily/<code>.parquet     个股日线：不复权 OHLC + 后复权 OHLC + 成交、换手、估值、停牌、ST、涨跌幅
    index/<code>.parquet     指数日线（基准 + 交易日历）
    universe/<name>.parquet  指数历史成分股快照（按月查询），用于避免幸存者偏差
    basics.parquet           股票名称、上市/退市日期
    manifest.json            每只股票最近一次检查更新的日期

价格：只下载不复权数据 + 后复权因子，后复权价 = 不复权价 × 当日适用的 backAdjustFactor
（已实测与 BaoStock 直接给出的后复权价一致）。收益用后复权价，"一手"的资金用不复权价。
"""

import json
import datetime as dt
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import pandas as pd

from ..i18n import L
from ..paths import CACHE_DIR

ROOT = CACHE_DIR / "stocks"

UNIVERSES = {
    "hs300": {"label": L("沪深300", "CSI 300"), "index": "sh.000300", "query": "query_hs300_stocks"},
    "zz500": {"label": L("中证500", "CSI 500"), "index": "sh.000905", "query": "query_zz500_stocks"},
    "sz50": {"label": L("上证50", "SSE 50"), "index": "sh.000016", "query": "query_sz50_stocks"},
    # 可转债：数据在 simplequant/bonds（东方财富 / 新浪），基准为中证转债指数；统一入口见 universe.py
    "cb": {"label": L("可转债（全市场）", "Convertible bonds (all)"), "index": "000832", "kind": "cb"},
}

DAILY_FIELDS = "date,open,high,low,close,preclose,volume,amount,turn,tradestatus,pctChg,isST,peTTM,pbMRQ,psTTM"
_RENAME = {"turn": "turnover", "peTTM": "pe", "pbMRQ": "pb", "psTTM": "ps", "pctChg": "pct_chg",
           "tradestatus": "tradable", "isST": "is_st"}


# ---------------- 纯函数：原始数据 → 存储格式（便于测试） ----------------
def process_daily(k: pd.DataFrame, adj: pd.DataFrame) -> pd.DataFrame:
    """
    :param k:   BaoStock 不复权日线（DAILY_FIELDS）
    :param adj: BaoStock 复权因子表（dividOperateDate, backAdjustFactor）
    """
    df = k.rename(columns=_RENAME).copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    num = [c for c in df.columns if c != "code"]
    df[num] = df[num].apply(pd.to_numeric, errors="coerce")
    df = df.rename(columns={"open": "raw_open", "high": "raw_high", "low": "raw_low", "close": "raw_close",
                            "preclose": "raw_preclose"})

    factor = pd.Series(1.0, index=df.index)
    if adj is not None and len(adj):
        a = adj.copy()
        a["dividOperateDate"] = pd.to_datetime(a["dividOperateDate"])
        a["backAdjustFactor"] = pd.to_numeric(a["backAdjustFactor"], errors="coerce")
        steps = a.dropna().set_index("dividOperateDate")["backAdjustFactor"].sort_index()
        steps = steps[~steps.index.duplicated(keep="last")]
        factor = steps.reindex(df.index.union(steps.index)).ffill().reindex(df.index).fillna(1.0)
    df["adj_factor"] = factor
    for c in ("open", "high", "low", "close"):
        df[c] = df[f"raw_{c}"] * df["adj_factor"]
    df["tradable"] = df["tradable"].fillna(0).astype(int)
    df["is_st"] = df["is_st"].fillna(0).astype(int)
    # 停牌日 BaoStock 给出的开高低收等于前收；这里保持原样，成交量为 0
    return df.drop(columns=[c for c in ("code",) if c in df.columns])


# ---------------- 下载（每个工作进程登录一次 BaoStock） ----------------
def _rows(rs) -> pd.DataFrame:
    out = []
    while rs.error_code == "0" and rs.next():
        out.append(rs.get_row_data())
    if rs.error_code != "0":
        raise ConnectionError(f"BaoStock: {rs.error_msg}")
    return pd.DataFrame(out, columns=rs.fields)


def _login():
    import baostock as bs
    lg = bs.login()
    if lg.error_code != "0":
        raise ConnectionError(f"BaoStock login failed / 登录失败: {lg.error_msg}")


def _login_quietly():
    """进程池的初始化函数：登录失败也不能抛异常（否则整个进程池崩溃），留给查询时重试"""
    try:
        _login()
    except Exception:  # noqa: BLE001
        pass


def _query(fn, *args, retries: int = 4, **kw) -> pd.DataFrame:
    """查询失败时等待、重新登录后重试（服务器在高并发下会断开连接）"""
    import time
    for attempt in range(retries):
        try:
            return _rows(fn(*args, **kw))
        except Exception:  # noqa: BLE001 - BaoStock 断线时可能抛出各种套接字异常
            if attempt == retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))
            try:
                _login()
            except Exception:  # noqa: BLE001
                pass


def _fetch_one(code: str, start: str, end: str) -> tuple[str, pd.DataFrame | None, str]:
    import baostock as bs
    try:
        k = _query(bs.query_history_k_data_plus, code, DAILY_FIELDS, start_date=start, end_date=end,
                   frequency="d", adjustflag="3")
        if k.empty:
            return code, None, ""
        adj = _query(bs.query_adjust_factor, code=code, start_date="1990-01-01", end_date=end)
        return code, process_daily(k, adj), ""
    except Exception as e:  # noqa: BLE001 - 单只失败不影响整体
        return code, None, f"{type(e).__name__}: {e}"


class StockStore:
    def __init__(self, root: Path = ROOT, fetcher=_fetch_one, login=_login, worker_init=_login_quietly):
        self.root = Path(root)
        self.fetcher, self.login, self.worker_init = fetcher, login, worker_init
        for sub in ("daily", "index", "universe"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    # ---------- 个股 ----------
    def _path(self, code: str) -> Path:
        return self.root / "daily" / f"{code}.parquet"

    def has(self, code: str) -> bool:
        return self._path(code).exists()

    def load(self, code: str) -> pd.DataFrame:
        return pd.read_parquet(self._path(code))

    def _manifest(self) -> dict:
        p = self.root / "manifest.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def _save_manifest(self, m: dict):
        (self.root / "manifest.json").write_text(json.dumps(m, indent=0), encoding="utf-8")

    def plan(self, codes: list[str], start: str, end: str) -> dict[str, tuple[str, str]]:
        """需要下载的 {代码: (起, 止)}：新股票下全量；已有的只补尾部；今天已检查过的跳过"""
        manifest, jobs = self._manifest(), {}
        for code in codes:
            if not self.has(code):
                jobs[code] = (start, end)
                continue
            checked = manifest.get(code, "")
            if checked >= end:
                continue
            first = self.load(code).index[0].strftime("%Y-%m-%d")
            if first > start and checked < start:          # 需要更早的数据：整段重下
                jobs[code] = (start, end)
            else:
                jobs[code] = (max(checked, first), end)
        return jobs

    def _run_jobs(self, jobs: dict, fetch, on_ok, workers: int, progress=None, checkpoint=None) -> dict[str, str]:
        """
        通用的并行下载执行器；返回仍然失败的 {代码: 错误信息}
        - fetch(code, *args) -> (code, 数据, 错误信息)；成功时调用 on_ok(code, 数据)
        - 每完成 10 只调用一次 checkpoint() 保存进度，中途中断后可从断点继续
        - 进程池崩溃时用更少的进程继续；最后把失败的单线程再试一次
        """
        errors, finished, n = {}, set(), len(jobs)

        def handle(code, data, err):
            if err:
                errors[code] = err
            else:
                errors.pop(code, None)
                on_ok(code, data)
            finished.add(code)
            if checkpoint and len(finished) % 10 == 0:
                checkpoint()
            if progress:
                progress(min(len(finished), n), n)

        def sequential(todo):
            try:
                self.login()
            except Exception:  # noqa: BLE001 - 查询时还会重试登录
                pass
            for code in todo:
                handle(*fetch(code, *jobs[code]))

        pending = list(jobs)
        while pending and workers > 1:
            try:
                with ProcessPoolExecutor(max_workers=workers, initializer=self.worker_init) as ex:
                    futures = [ex.submit(fetch, code, *jobs[code]) for code in pending]
                    for f in as_completed(futures):
                        handle(*f.result())
                pending = []
            except BrokenProcessPool:
                pending = [c for c in pending if c not in finished]
                workers //= 2
        if pending:
            sequential(pending)
        if errors:                     # 失败的单线程再试一次
            retry = list(errors)
            finished -= set(retry)
            sequential(retry)
        if checkpoint:
            checkpoint()
        return errors

    def update(self, codes: list[str], start: str, end: str, workers: int = 4, progress=None) -> dict[str, str]:
        """个股日线：并行下载 / 增量更新"""
        jobs = self.plan(codes, start, end)
        if not jobs:
            return {}
        manifest = self._manifest()

        def on_ok(code, df):
            if df is not None and len(df):
                if self.has(code):
                    old = self.load(code)
                    df = pd.concat([old[old.index < df.index[0]], df])
                df.to_parquet(self._path(code), compression="zstd")
            # 记录数据实际到达的日期（不是查询截止日）：收盘前运行过一次时，收盘后再运行仍会补取当天数据
            if self.has(code):
                manifest[code] = max(manifest.get(code, ""), self.load(code).index[-1].strftime("%Y-%m-%d"))

        return self._run_jobs(jobs, self.fetcher, on_ok, workers, progress, lambda: self._save_manifest(manifest))

    # ---------- 财务数据 ----------
    def _fin_path(self, code: str) -> Path:
        return self.root / "fin" / f"{code}.parquet"

    def has_fin(self, code: str) -> bool:
        return self._fin_path(code).exists()

    def load_fin(self, code: str) -> pd.DataFrame:
        return pd.read_parquet(self._fin_path(code))

    def plan_fundamentals(self, codes: list[str], start_year: int, today: dt.date | None = None) -> dict:
        """新股票下全部季度；已有的重下最近两个季度（有公司延迟披露）及之后的季度；今天已检查过的跳过"""
        from .fundamentals import quarters
        today = today or dt.date.today()
        qs = quarters(start_year, today)
        manifest = self._fin_manifest()
        jobs = {}
        for code in codes:
            if not self.has_fin(code):
                jobs[code] = (qs,)
            elif manifest.get(code) != today.isoformat():
                have = self.load_fin(code)
                last = have["stat_date"].max() if len(have) else pd.Timestamp(f"{start_year}-01-01")
                cut = (last - pd.DateOffset(months=3))
                jobs[code] = ([(y, q) for y, q in qs if pd.Timestamp(y, q * 3, 1) >= cut.replace(day=1)] or qs[-2:],)
        return jobs

    def _fin_manifest(self) -> dict:
        p = self.root / "fin_manifest.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def update_fundamentals(self, codes: list[str], start_year: int = 2018, workers: int = 4, progress=None,
                            fetch=None) -> dict[str, str]:
        from .fundamentals import fetch_profit
        (self.root / "fin").mkdir(exist_ok=True)
        jobs = self.plan_fundamentals(codes, start_year)
        if not jobs:
            return {}
        manifest, today_s = self._fin_manifest(), dt.date.today().isoformat()

        def on_ok(code, df):
            if self.has_fin(code) and df is not None:
                old = self.load_fin(code)
                df = pd.concat([old[~old["stat_date"].isin(df["stat_date"])], df]).sort_values("stat_date")
            if df is not None:
                df.reset_index(drop=True).to_parquet(self._fin_path(code), compression="zstd")
            manifest[code] = today_s

        def save():
            (self.root / "fin_manifest.json").write_text(json.dumps(manifest, indent=0), encoding="utf-8")

        return self._run_jobs(jobs, fetch or fetch_profit, on_ok, workers, progress, save)

    # ---------- 分红送转 ----------
    def _div_path(self, code: str) -> Path:
        return self.root / "div" / f"{code}.parquet"

    def has_div(self, code: str) -> bool:
        return self._div_path(code).exists()

    def load_div(self, code: str) -> pd.DataFrame:
        return pd.read_parquet(self._div_path(code))

    def _div_manifest(self) -> dict:
        p = self.root / "div_manifest.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def plan_dividends(self, codes: list[str], start_year: int, today: dt.date | None = None) -> dict:
        """新股票下全部年份；已有的重下去年和今年（年报分红集中在下半年实施）；今天已检查过的跳过"""
        from .dividends import years
        today = today or dt.date.today()
        ys = years(start_year, today)
        manifest, jobs = self._div_manifest(), {}
        for code in codes:
            if not self.has_div(code):
                jobs[code] = (ys,)
            elif manifest.get(code) != today.isoformat():
                jobs[code] = ([y for y in ys if y >= today.year - 1],)
        return jobs

    def _rights_path(self) -> Path:
        return self.root / "rights.parquet"

    def update_rights(self, fetch=None) -> str:
        """配股表（全市场一次下载，用来区分漏记的分红和配股）；每天最多一次。返回错误信息（成功为空）"""
        from .dividends import fetch_rights
        p = self._rights_path()
        if p.exists() and dt.date.fromtimestamp(p.stat().st_mtime) >= dt.date.today():
            return ""
        try:
            df = (fetch or fetch_rights)()
        except Exception as e:  # noqa: BLE001 - 东方财富接口不稳定：沿用旧表
            return f"{type(e).__name__}: {e}"
        self.root.mkdir(parents=True, exist_ok=True)
        df.to_parquet(p)
        return ""

    def load_rights(self) -> dict | None:
        """代码 -> 配股的股权登记日列表；从没下载成功过时为 None"""
        p = self._rights_path()
        if not p.exists():
            return None
        df = pd.read_parquet(p)
        return {c: list(g["record_date"]) for c, g in df.groupby("code")}

    def update_dividends(self, codes: list[str], start_year: int = 2018, workers: int = 4, progress=None,
                         fetch=None) -> dict[str, str]:
        from .dividends import fetch_dividends
        (self.root / "div").mkdir(exist_ok=True)
        if fetch is None:
            self.update_rights()
        jobs = self.plan_dividends(codes, start_year)
        if not jobs:
            return {}
        manifest, today_s = self._div_manifest(), dt.date.today().isoformat()

        def on_ok(code, df):
            if df is None:
                return
            if self.has_div(code):
                first_year = min(jobs[code][0])
                old = self.load_div(code)
                df = pd.concat([old[old["ex_date"].dt.year < first_year], df]).sort_values("ex_date")
            df.reset_index(drop=True).to_parquet(self._div_path(code), compression="zstd")
            manifest[code] = today_s

        def save():
            (self.root / "div_manifest.json").write_text(json.dumps(manifest, indent=0), encoding="utf-8")

        return self._run_jobs(jobs, fetch or fetch_dividends, on_ok, workers, progress, save)

    # ---------- 行业 ----------
    def update_industry(self) -> pd.DataFrame:
        """证监会行业分类（BaoStock 只提供当前分类，没有历史）"""
        import baostock as bs
        self.login()
        df = _query(bs.query_stock_industry)[["code", "industry"]]
        df["industry"] = df["industry"].replace("", "未分类")
        df.to_parquet(self.root / "industry.parquet")
        return df

    def load_industry(self) -> pd.Series:
        p = self.root / "industry.parquet"
        if not p.exists():
            return pd.Series(dtype=str)
        df = pd.read_parquet(p)
        return df.set_index("code")["industry"]

    # ---------- 数据包：导出 / 导入 ----------
    def export_zip(self) -> bytes:
        """把全部选股数据打成 zip（parquet 已压缩，zip 只做归档）"""
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
            for f in sorted(self.root.rglob("*")):
                if f.is_file():
                    z.write(f, f.relative_to(self.root).as_posix())
        return buf.getvalue()

    def import_zip(self, fileobj) -> int:
        """
        导入数据包：覆盖同名文件；进度记录取两边较新的日期。返回导入的个股数量。
        只接受本数据目录结构内的文件，拒绝绝对路径和 ".." 等越界路径。
        """
        import zipfile
        allowed_dirs = {"daily", "index", "universe", "fin", "div"}
        allowed_files = {"basics.parquet", "manifest.json", "industry.parquet", "fin_manifest.json",
                         "div_manifest.json"}
        extra_manifests = ("fin_manifest.json", "div_manifest.json")
        with zipfile.ZipFile(fileobj) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            for n in names:
                parts = Path(n).parts
                ok = (not Path(n).is_absolute() and ".." not in parts and
                      ((len(parts) == 2 and parts[0] in allowed_dirs and n.endswith(".parquet"))
                       or (len(parts) == 1 and parts[0] in allowed_files)))
                if not ok:
                    raise ValueError(f"unexpected file in data pack / 数据包中有不认识的文件: {n}")
            incoming = json.loads(z.read("manifest.json")) if "manifest.json" in names else {}
            incoming_extra = {m: json.loads(z.read(m)) for m in extra_manifests if m in names}
            for n in names:
                if n not in ("manifest.json", *extra_manifests):   # 进度记录需要合并，不能直接覆盖
                    target = self.root / n
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(z.read(n))
        merged = self._manifest()
        for code, d in incoming.items():
            merged[code] = max(d, merged.get(code, ""))
        self._save_manifest(merged)
        for m, incoming_m in incoming_extra.items():
            p = self.root / m
            have = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
            for code, d in incoming_m.items():
                have[code] = max(d, have.get(code, ""))
            p.write_text(json.dumps(have, indent=0), encoding="utf-8")
        return sum(1 for n in names if n.startswith("daily/"))

    # ---------- 指数（基准 + 交易日历） ----------
    def update_index(self, code: str, start: str, end: str) -> pd.DataFrame:
        import baostock as bs
        self.login()
        df = _query(bs.query_history_k_data_plus, code, "date,open,close", start_date=start, end_date=end,
                    frequency="d")
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")[["open", "close"]].apply(pd.to_numeric, errors="coerce")
        path = self.root / "index" / f"{code}.parquet"
        if path.exists():           # 与已有的合并：下载较短的区间不会把更早的交易日删掉（指数决定交易日历）
            old = pd.read_parquet(path)
            df = pd.concat([old[~old.index.isin(df.index)], df]).sort_index()
        df.to_parquet(path)
        return df

    def load_index(self, code: str) -> pd.DataFrame:
        return pd.read_parquet(self.root / "index" / f"{code}.parquet")

    # ---------- 历史成分股 ----------
    def update_universe(self, name: str, start: str, end: str, progress=None) -> pd.DataFrame:
        """在区间内每月月末查询一次成分股快照（指数一般半年调整一次，按月查足够）"""
        import baostock as bs
        fn = getattr(bs, UNIVERSES[name]["query"])
        path = self.root / "universe" / f"{name}.parquet"
        old = pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=["date", "code", "name"])
        done = set(pd.to_datetime(old["date"]).dt.strftime("%Y-%m-%d")) if len(old) else set()
        dates = [start] + [d.strftime("%Y-%m-%d") for d in pd.date_range(start, end, freq="ME")] + [end]
        todo = [d for d in dict.fromkeys(dates) if d not in done]
        self.login()
        parts = [old]
        for i, d in enumerate(todo, 1):
            snap = _query(fn, date=d)
            parts.append(pd.DataFrame({"date": pd.Timestamp(d), "code": snap["code"], "name": snap["code_name"]}))
            if progress:
                progress(i, len(todo))
        uni = pd.concat(parts, ignore_index=True)
        uni["date"] = pd.to_datetime(uni["date"])
        uni = uni.drop_duplicates(["date", "code"]).sort_values(["date", "code"]).reset_index(drop=True)
        uni.to_parquet(path)
        return uni

    def load_universe(self, name: str) -> pd.DataFrame:
        return pd.read_parquet(self.root / "universe" / f"{name}.parquet")

    def has_universe(self, name: str) -> bool:
        return (self.root / "universe" / f"{name}.parquet").exists()

    # ---------- 基本信息 ----------
    def update_basics(self) -> pd.DataFrame:
        import baostock as bs
        self.login()
        df = _query(bs.query_stock_basic)
        df = df[df["type"] == "1"][["code", "code_name", "ipoDate", "outDate"]].rename(
            columns={"code_name": "name", "ipoDate": "ipo_date", "outDate": "out_date"})
        df["ipo_date"] = pd.to_datetime(df["ipo_date"], errors="coerce")
        df["out_date"] = pd.to_datetime(df["out_date"], errors="coerce")
        df.to_parquet(self.root / "basics.parquet")
        return df

    def load_basics(self) -> pd.DataFrame:
        return pd.read_parquet(self.root / "basics.parquet")


def today() -> str:
    return dt.date.today().strftime("%Y-%m-%d")
