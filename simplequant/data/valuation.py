"""
宽基指数的估值（市盈率 TTM、市净率），附在 ETF / 指数日线上

- 来源：乐咕乐股（AKShare 的 stock_index_pe_lg / stock_index_pb_lg），只有 12 个宽基指数，
  历史数据是每月月末一个点、再加最近一个交易日，没有逐日数据
- 附到日线上时每天取「当天及以前最近公布的那个值」（月末的值是当天收盘算出的，不含未来信息），
  所以估值一个月才变一次；用来看估值高低（百分位）足够，不适合做短线信号
- ETF 用它跟踪的指数的估值（只收录常见的宽基 ETF）
"""

import logging

import pandas as pd

log = logging.getLogger(__name__)

# 乐咕乐股支持的指数：代码 -> AKShare 用的名称
INDEXES = {"000016": "上证50", "000300": "沪深300", "000009": "上证380", "399673": "创业板50", "000905": "中证500",
           "000010": "上证180", "399324": "深证红利", "399330": "深证100", "000852": "中证1000", "000015": "上证红利",
           "000903": "中证100", "000906": "中证800"}
# 常见宽基 ETF -> 跟踪的指数
ETF_INDEX = {"510050": "000016", "510300": "000300", "510310": "000300", "510330": "000300", "159919": "000300",
             "510500": "000905", "510510": "000905", "159922": "000905", "512100": "000852", "159949": "399673",
             "510180": "000010", "510880": "000015", "159905": "399324", "159901": "399330"}


def index_of(symbol: str, asset: str) -> str | None:
    """这份数据可以附上哪个指数的估值；没有时 None"""
    code = ETF_INDEX.get(symbol) if asset == "etf" else symbol if asset == "index" else None
    return code if code in INDEXES else None


def index_valuation(code: str) -> pd.DataFrame:
    """指数的估值表：index = 日期，列 pe（滚动市盈率）、pb（市净率）"""
    import akshare as ak
    from . import net
    name = INDEXES[code]
    pe = net.call("legu", lambda: ak.stock_index_pe_lg(symbol=name))
    pb = net.call("legu", lambda: ak.stock_index_pb_lg(symbol=name))
    out = pd.concat([pe.set_index(pd.to_datetime(pe["日期"]))["滚动市盈率"].rename("pe"),
                     pb.set_index(pd.to_datetime(pb["日期"]))["市净率"].rename("pb")], axis=1)
    out = out.apply(pd.to_numeric, errors="coerce")
    return out[~out.index.duplicated(keep="last")].sort_index()


def attach(df: pd.DataFrame, val: pd.DataFrame) -> pd.DataFrame:
    """每根 K 线取当天及以前最近一次的估值；第一次估值之前为空。原有的 pe / pb 列被替换"""
    out = df.drop(columns=[c for c in ("pe", "pb") if c in df.columns])
    v = val.reindex(val.index.union(out.index)).ffill().reindex(out.index)
    for c in ("pe", "pb"):
        if c in v and v[c].notna().any():
            out[c] = v[c].to_numpy()
    return out


def try_attach(df: pd.DataFrame, symbol: str, asset: str) -> pd.DataFrame:
    """能附上估值就附上（df.attrs["valuation"] = 指数代码）；接口失败只记日志，不影响行情下载"""
    code = index_of(symbol, asset)
    if code is None:
        return df
    try:
        out = attach(df, index_valuation(code))
    except Exception as e:  # noqa: BLE001 - 估值是附加信息
        log.warning("index valuation for %s (%s) failed: %s", symbol, code, e)
        return df
    out.attrs = {**df.attrs, "valuation": code}
    return out
