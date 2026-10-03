"""
可转债数据层

目录：<数据目录>/data_cache/cb/
    list.parquet          全部可转债（含已退市，东方财富）：代码、简称、正股、上市日期、发行规模、发行时评级
    info.parquet          条款（每只一行，东方财富）：起息日、期限、退市日、付息日、各年票面利率、赎回公告
    daily/<代码>.parquet   日线：不复权开高低收、成交量（张，新浪）+ 纯债价值、转股价值、转股价、剩余规模（东方财富）
    index.parquet         中证转债指数 000832（基准 + 交易日历，中证指数公司）
    manifest.json         每只转债最近一次更新到的日期

接口实测（2026-10-01）：
- 新浪日线不复权、成交量单位是「张」；个别转债缺上市后的前几个月（东方财富的价值分析里有收盘价）
- 东方财富的价值分析含每日转股价（下修历史）；剩余规模只有部分转债有
- 转债按全价交易，付息日价格下跳；票面利率在条款里（文字），由 panel.py 换算成复权因子
"""

import datetime as dt
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

from ..data import net
from ..data.base import with_retry
from ..paths import CACHE_DIR

ROOT = CACHE_DIR / "cb"
INDEX_CODE = "000832"            # 中证转债
EM_URL = "https://datacenter-web.eastmoney.com/api/data/get"
EM_TOKEN = "894050c76af8597a853f5b408b759f5d"
VALUE_FIELDS = "DATE,FCLOSE,PUREBONDVALUE,SWAPVALUE,SWAPPRICE,SYFE"
DAILY_COLUMNS = ["raw_open", "raw_high", "raw_low", "raw_close", "volume", "bond_value", "conv_value",
                 "conv_price", "remain_size", "sina"]

INFO_FIELDS = {
    "SECURITY_CODE": "code", "SECURITY_NAME_ABBR": "name", "CONVERT_STOCK_CODE": "stock_code",
    "LISTING_DATE": "listing_date", "DELIST_DATE": "delist_date", "VALUE_DATE": "value_date",
    "BOND_EXPIRE": "term_years", "PAY_INTEREST_DAY": "pay_day", "INTEREST_RATE_EXPLAIN": "coupon_text",
    "RATING": "rating", "ACTUAL_ISSUE_SCALE": "issue_size", "EXECUTE_REASON_SH": "redeem_reason",
    "NOTICE_DATE_SH": "redeem_notice", "EXECUTE_PRICE_SH": "redeem_price", "INITIAL_TRANSFER_PRICE": "init_conv_price",
}
INFO_DATES = ("listing_date", "delist_date", "value_date", "redeem_notice")
REDEEM_CALL, REDEEM_MATURITY = "4", "5"     # 东方财富 EXECUTE_REASON_SH：4 = 有条件赎回（强赎），5 = 到期赎回


# ---------------- 纯函数（便于测试） ----------------
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _year_no(s: str) -> int | None:
    if s.isdigit():
        return int(s)
    if s in _CN_NUM:
        return _CN_NUM[s]
    if len(s) == 2 and s[0] == "十" and s[1] in _CN_NUM:
        return 10 + _CN_NUM[s[1]]
    return None


def parse_coupons(text: str, years: int | None = None) -> list[float] | None:
    """
    条款里的各年票面利率（%）。常见写法：
    「第一年为0.2%、第二年为0.5%……」「第一年0.30%、第二年0.50%……」「第1年0.3%……」
    「第一年至第六年分别为0.3%、0.5%、1.0%、1.5%、1.8%、2.0%」
    years 为空时按解析出的连续年数（第 1 年到第 N 年）；解析不出或年数对不上时返回 None。
    注意东方财富的 BOND_EXPIRE 对提前赎回的转债是实际存续年数（如 4.663），不能当作期限
    """
    if not isinstance(text, str):
        return None
    text = text.replace("％", "%").replace(" ", "")
    found = {}
    for m in re.finditer(r"第(十[一二三四五六七八九]?|[一二三四五六七八九]|\d{1,2})(?:个计息)?年?(?:度)?[^%第]*?"
                         r"(\d+(?:\.\d+)?)%", text):
        n = _year_no(m.group(1))
        if n and n not in found:
            found[n] = float(m.group(2))
    n = years or len(found)
    if n and len(found) == n and sorted(found) == list(range(1, n + 1)):
        return [found[i] for i in range(1, n + 1)]
    m = re.search(r"(?:至第([一二三四五六七八九十\d]{1,2})年)?[^%]*?分别为(.*)", text)
    if m:
        rates = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)%", m.group(2))]
        n = years or (_year_no(m.group(1)) if m.group(1) else len(rates))
        if n and len(rates) >= n:
            return rates[:n]
    return None


def merge_daily(sina: pd.DataFrame | None, value: pd.DataFrame | None) -> pd.DataFrame:
    """
    新浪日线 + 东方财富价值分析 → 存储格式（日期索引）。
    东方财富有收盘价而新浪缺的日子也保留（开高低收都用收盘价，sina=0 表示这天只有收盘价，回测里不交易）；
    东方财富上市前那几天（收盘价为空）去掉。
    """
    parts = []
    if sina is not None and len(sina):
        s = sina.copy()
        s["date"] = pd.to_datetime(s["date"])
        s = s.set_index("date").sort_index()
        s = s.rename(columns={"open": "raw_open", "high": "raw_high", "low": "raw_low", "close": "raw_close"})
        s = s[["raw_open", "raw_high", "raw_low", "raw_close", "volume"]].apply(pd.to_numeric, errors="coerce")
        s["sina"] = 1.0
        parts.append(s[s["raw_close"] > 0])
    df = parts[0] if parts else pd.DataFrame(columns=["raw_open", "raw_high", "raw_low", "raw_close", "volume",
                                                      "sina"], dtype=float)
    if value is not None and len(value):
        v = value.copy()
        v["date"] = pd.to_datetime(v["DATE"])
        v = v.set_index("date").sort_index()
        v = v[~v.index.duplicated(keep="last")]
        v = v.rename(columns={"FCLOSE": "close_em", "PUREBONDVALUE": "bond_value", "SWAPVALUE": "conv_value",
                              "SWAPPRICE": "conv_price", "SYFE": "remain_size"})
        cols = ["close_em", "bond_value", "conv_value", "conv_price", "remain_size"]
        v = v.reindex(columns=cols).apply(pd.to_numeric, errors="coerce")
        v["remain_size"] = v["remain_size"] / 1e8                     # 元 → 亿元
        extra = v.index.difference(df.index)
        extra = extra[v.loc[extra, "close_em"].notna() & (v.loc[extra, "close_em"] > 0)]
        if len(extra):
            fill = pd.DataFrame(index=extra)
            for c in ("raw_open", "raw_high", "raw_low", "raw_close"):
                fill[c] = v.loc[extra, "close_em"]
            fill["volume"] = float("nan")
            fill["sina"] = 0.0
            df = pd.concat([df, fill]).sort_index() if len(df) else fill
        df = df.join(v[cols[1:]], how="left")
    for c in DAILY_COLUMNS:
        if c not in df.columns:
            df[c] = float("nan")
    df.index.name = "date"
    return df[DAILY_COLUMNS].astype(float)


def process_info(raw: dict) -> dict:
    out = {new: raw.get(old) for old, new in INFO_FIELDS.items()}
    for k in INFO_DATES:
        out[k] = pd.to_datetime(out[k], errors="coerce")
    out["code"] = str(out["code"])
    term = pd.to_numeric(out["term_years"], errors="coerce")
    out["term_years"] = float(term) if pd.notna(term) else None        # 早年的转债有 2.4 年这样的期限
    out["redeem_reason"] = str(out["redeem_reason"]) if out["redeem_reason"] is not None else ""
    for k in ("issue_size", "redeem_price", "init_conv_price"):
        out[k] = pd.to_numeric(out[k], errors="coerce")
    coupons = parse_coupons(out["coupon_text"])
    out["coupons"] = ",".join(f"{c:g}" for c in coupons) if coupons else ""
    # 到期日：起息日 + 票面利率的年数（提前赎回的转债 BOND_EXPIRE 是实际存续年数，不能用）
    out["maturity"] = out["value_date"] + pd.DateOffset(years=len(coupons))         if coupons and pd.notna(out["value_date"]) else pd.NaT
    return out


def sina_symbol(code: str) -> str:
    return ("sh" if code.startswith("11") else "sz") + code


# ---------------- 网络 ----------------
def fetch_list() -> pd.DataFrame:
    import akshare as ak
    raw = pd.DataFrame(with_retry(ak.bond_zh_cov, retries=4, wait=2.0))
    df = pd.DataFrame({
        "code": raw["债券代码"].astype(str), "name": raw["债券简称"], "stock_code": raw["正股代码"].astype(str),
        "stock_name": raw["正股简称"], "listing_date": pd.to_datetime(raw["上市时间"], errors="coerce"),
        "issue_size": pd.to_numeric(raw["发行规模"], errors="coerce"), "rating": raw["信用评级"],
    })
    return df[df["code"].str.match(r"^1[12]\d{4}$")].reset_index(drop=True)


def fetch_daily(code: str) -> pd.DataFrame:
    import akshare as ak

    try:      # 与 ETF / 股票日线共用新浪的限速与暂停（data/net.py）
        return pd.DataFrame(net.call("sina", lambda: ak.bond_zh_hs_cov_daily(symbol=sina_symbol(code)),
                                     retries=3, wait=2.0))
    except (KeyError, TypeError, IndexError):       # 新浪没有这只（返回内容解析不出）
        return pd.DataFrame()


def fetch_value(code: str, since: str | None = None) -> pd.DataFrame:
    """东方财富价值分析，只取需要的字段（全字段约 300 KB/只）；since：只取这天及以后的（增量更新）"""
    import requests
    flt = f'(zcode="{code}")' + (f"(DATE>='{since}')" if since else "")
    params = {"sty": VALUE_FIELDS, "token": EM_TOKEN, "st": "date", "sr": "1", "source": "WEB",
              "type": "RPTA_WEB_KZZ_LS", "filter": flt, "p": "1", "ps": "8000"}

    def get():
        r = requests.get(EM_URL, params=params, timeout=20)
        r.raise_for_status()
        return r.json()
    js = with_retry(get, retries=3, wait=2.0)
    data = (js.get("result") or {}).get("data") or []
    return pd.DataFrame(data, columns=VALUE_FIELDS.split(",")) if data else pd.DataFrame(columns=VALUE_FIELDS.split(","))


def fetch_info(code: str) -> dict:
    import akshare as ak
    df = with_retry(lambda: ak.bond_zh_cov_info(symbol=code, indicator="基本信息"), retries=3, wait=2.0)
    if df is None or not len(df):
        raise ValueError(f"no terms for {code} / 没有 {code} 的条款")
    return process_info(df.iloc[0].to_dict())


def fetch_index(start: str, end: str) -> pd.DataFrame:
    import akshare as ak
    raw = with_retry(lambda: ak.stock_zh_index_hist_csindex(symbol=INDEX_CODE, start_date=start.replace("-", ""),
                                                            end_date=end.replace("-", "")), retries=4, wait=2.0)
    df = pd.DataFrame({"date": pd.to_datetime(raw["日期"]), "open": pd.to_numeric(raw["开盘"], errors="coerce"),
                       "close": pd.to_numeric(raw["收盘"], errors="coerce")})
    df = df.dropna().drop_duplicates("date", keep="last").set_index("date").sort_index()
    return df[df.index.dayofweek < 5]


# ---------------- 存储 ----------------
class CBStore:
    def __init__(self, root: Path = ROOT, fetchers: dict | None = None):
        self.root = Path(root)
        self.f = {"list": fetch_list, "daily": fetch_daily, "value": fetch_value, "info": fetch_info,
                  "index": fetch_index, **(fetchers or {})}
        (self.root / "daily").mkdir(parents=True, exist_ok=True)

    # ---------- 列表与条款 ----------
    def update_list(self) -> pd.DataFrame:
        df = self.f["list"]()
        df.to_parquet(self.root / "list.parquet")
        return df

    def load_list(self) -> pd.DataFrame:
        return pd.read_parquet(self.root / "list.parquet")

    def has_list(self) -> bool:
        return (self.root / "list.parquet").exists()

    def load_info(self) -> pd.DataFrame:
        p = self.root / "info.parquet"
        if not p.exists():
            return pd.DataFrame()
        info = pd.read_parquet(p).set_index("code")
        # 解析规则改进后，旧数据里没解析出的票面利率重新解析一次（不用重新下载）
        miss = info["coupons"].fillna("").eq("") & info["coupon_text"].notna()
        for c in info.index[miss]:
            coupons = parse_coupons(info.at[c, "coupon_text"])
            if coupons:
                info.at[c, "coupons"] = ",".join(f"{x:g}" for x in coupons)
                if pd.notna(info.at[c, "value_date"]):
                    info.at[c, "maturity"] = info.at[c, "value_date"] + pd.DateOffset(years=len(coupons))
        return info

    def update_info(self, codes: list[str], refresh_after: dt.date | None = None, workers: int = 4,
                    progress=None) -> dict[str, str]:
        """
        条款：新转债都下；已有的只更新还没退市的（转股价下修、强赎公告会变）。
        refresh_after：已有记录且退市日早于这天的不再更新
        """
        have = self.load_info()
        today = pd.Timestamp(dt.date.today())
        todo = []
        for c in codes:
            if c not in have.index:
                todo.append(c)
                continue
            delist = have.at[c, "delist_date"]
            if pd.isna(delist) or delist >= today:
                todo.append(c)
        rows, errors = {}, {}

        def one(c):
            try:
                return c, self.f["info"](c), ""
            except Exception as e:  # noqa: BLE001 - 单只失败不影响整体
                return c, None, f"{type(e).__name__}: {e}"
        self._parallel(todo, one, lambda c, r: rows.__setitem__(c, r), errors, workers, progress)
        if rows:
            new = pd.DataFrame(list(rows.values())).set_index("code")
            merged = pd.concat([have[~have.index.isin(new.index)], new]) if len(have) else new
            merged.reset_index().to_parquet(self.root / "info.parquet")
        return errors

    # ---------- 日线 ----------
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

    def wanted(self, start: str) -> list[str]:
        """需要日线的转债：已上市，且退市日不早于 start（退市日未知的也要）"""
        info = self.load_info()
        lst = self.load_list().set_index("code")
        listing = lst["listing_date"]
        if len(info):
            listing = info["listing_date"].reindex(lst.index).fillna(listing)
        delist = info["delist_date"].reindex(lst.index) if len(info) else pd.Series(pd.NaT, index=lst.index)
        ok = listing.notna() & (listing <= pd.Timestamp(dt.date.today())) & \
            (delist.isna() | (delist >= pd.Timestamp(start)))
        return sorted(lst.index[ok])

    def plan(self, codes: list[str], end: str) -> dict[str, str | None]:
        """{代码: 东方财富增量起点（None = 全量）}；已退市且已下完的、今天已更新到 end 的跳过"""
        manifest, info = self._manifest(), self.load_info()
        jobs = {}
        for c in codes:
            if not self.has(c):
                jobs[c] = None
                continue
            checked = manifest.get(c, "")
            delist = info["delist_date"].get(c) if len(info) and c in info.index else None
            if delist is not None and pd.notna(delist) and checked and pd.Timestamp(checked) >= delist - pd.Timedelta(days=10):
                continue                         # 已退市，数据已到退市前
            if checked >= end:
                continue
            jobs[c] = (pd.Timestamp(checked) - pd.Timedelta(days=10)).strftime("%Y-%m-%d") if checked else None
        return jobs

    def update(self, codes: list[str], end: str, workers: int = 3, progress=None) -> dict[str, str]:
        jobs = self.plan(codes, end)
        manifest, errors = self._manifest(), {}

        def one(c):
            try:
                sina = self.f["daily"](c)
                value = self.f["value"](c, jobs[c])
                return c, (sina, value), ""
            except Exception as e:  # noqa: BLE001
                return c, None, f"{type(e).__name__}: {e}"

        def ok(c, data):
            sina, value = data
            since = jobs[c]
            df = merge_daily(sina, value)
            if since and self.has(c):            # 增量：增量起点之前沿用已有数据（东方财富只取了起点之后的）
                old = self.load(c)
                df = pd.concat([old[old.index < pd.Timestamp(since)], df[df.index >= pd.Timestamp(since)]])
            if len(df):
                df.to_parquet(self._path(c), compression="zstd")
                manifest[c] = df.index[-1].strftime("%Y-%m-%d")
            else:
                manifest[c] = end
        self._parallel(list(jobs), one, ok, errors, workers, progress, checkpoint=lambda: self._save_manifest(manifest))
        self._save_manifest(manifest)
        return errors

    # ---------- 指数 ----------
    def update_index(self, start: str, end: str) -> pd.DataFrame:
        df = self.f["index"](start, end)
        try:
            from ..paper.calendar import load_calendar
            cal = load_calendar()
            inside = (df.index >= cal[0]) & (df.index <= cal[-1])
            df = df[~inside | df.index.isin(cal)]          # 中证官网偶尔有节假日的重复行
        except Exception:  # noqa: BLE001 - 取不到日历时只去掉周末
            pass
        df.to_parquet(self.root / "index.parquet")
        return df

    def load_index(self) -> pd.DataFrame:
        return pd.read_parquet(self.root / "index.parquet")

    def has_index(self) -> bool:
        return (self.root / "index.parquet").exists()

    def ready(self) -> bool:
        return self.has_list() and self.has_index() and (self.root / "info.parquet").exists()

    def data_start(self) -> str | None:
        """下载时选的起始日（这天之前退市的转债没有下载，面板不应早于它）"""
        p = self.root / "meta.json"
        return json.loads(p.read_text(encoding="utf-8")).get("start") if p.exists() else None

    def _set_start(self, start: str):
        old = self.data_start()
        (self.root / "meta.json").write_text(json.dumps({"start": min(old, start) if old else start}),
                                             encoding="utf-8")

    # ---------- 并行执行 ----------
    @staticmethod
    def _parallel(todo, one, on_ok, errors, workers, progress=None, checkpoint=None):
        n, done = len(todo), 0
        if not n:
            return
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futures = [ex.submit(one, c) for c in todo]
            for f in as_completed(futures):
                c, data, err = f.result()
                if err:
                    errors[c] = err
                else:
                    on_ok(c, data)
                done += 1
                if checkpoint and done % 20 == 0:
                    checkpoint()
                if progress:
                    progress(done, n)
        retry = list(errors)                     # 失败的单线程再试一次
        for c in retry:
            c, data, err = one(c)
            if not err:
                errors.pop(c, None)
                on_ok(c, data)

    # ---------- 一次更新全部 ----------
    def update_all(self, start: str, end: str, workers: int = 3, progress=None) -> dict[str, str]:
        """列表 → 条款 → 指数 → 日线。progress(步骤, 完成数, 总数)"""
        def step(name):
            return (lambda i, n: progress(name, i, n)) if progress else None
        if progress:
            progress("list", 0, 1)
        codes = sorted(self.update_list()["code"])
        errors = self.update_info(codes, workers=workers + 1, progress=step("info"))
        if progress:
            progress("index", 0, 1)
        self.update_index("2010-01-01", end)
        errors.update(self.update(self.wanted(start), end, workers=workers, progress=step("daily")))
        self._set_start(start)
        return errors


def today() -> str:
    return dt.date.today().strftime("%Y-%m-%d")
