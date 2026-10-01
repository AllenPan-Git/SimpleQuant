"""
A 股各品种的交易制度

T+0（当天买入当天可卖）：债券、可转债、债券 ETF、货币 ETF、黄金 ETF、商品期货 ETF、跨境 ETF（含港股通 ETF）
T+1：股票、境内股票 ETF、REITs 等其余品种

判断依据是标的名称：数据库里的名称一般是「代码 简称」（如 "511010 国债ETF"），
CSV 导入的可能只有简称或文件名。代码段能确定的按代码段判断，不能确定的（深市 159、沪市 51x 等）再看简称里的关键词。
"""

import re

# 整个代码段都是 T+0 的品种
T0_PREFIXES = (
    "511",              # 沪市债券 ETF、货币 ETF
    "513",              # 沪市跨境 ETF（含港股通）
    "518",              # 沪市黄金 ETF
    "01",               # 沪市国债
    "10",               # 深市国债、地方债
    "11",               # 沪市可转债（110 / 111 / 113 / 118）、深市公司债
    "12",               # 深市可转债（123 / 127 / 128）
)

# 代码段里 T+0 和 T+1 混在一起时，常用的 T+0 品种
T0_CODES = frozenset({
    "510900",           # H 股 ETF
    "159920",           # 恒生 ETF
    "159941",           # 纳指 ETF
    "159934", "159937",  # 黄金 ETF
    "159985",           # 豆粕 ETF
    "159981",           # 能源化工 ETF
    "159980",           # 有色金属期货 ETF
    "159972", "159816",  # 地方政府债 ETF
})

# 只对基金代码段（5 开头、15 / 16 开头）或没有代码的名称使用；股票不看关键词
T0_KEYWORDS = ("债", "黄金", "上海金", "豆粕", "能源化工", "期货", "货币",
               "纳指", "纳斯达克", "标普", "道琼斯", "日经", "德国", "法国", "沙特", "东南亚", "亚太",
               "恒生", "港股", "中概", "H股", "QDII")
T1_KEYWORDS = ("黄金股",)          # 黄金股 ETF 投资的是境内股票

_CODE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


def symbol_of(name: str) -> str:
    """名称里的 6 位代码；没有则返回空串"""
    m = _CODE.search(name or "")
    return m.group(1) if m else ""


def _keyword_t0(name: str) -> bool:
    if any(k in name for k in T1_KEYWORDS):
        return False
    return any(k in name for k in T0_KEYWORDS)


def is_t0(name: str) -> bool:
    """该标的是否实行 T+0"""
    code = symbol_of(name)
    if not code:
        return _keyword_t0(name or "")
    if code in T0_CODES or code.startswith(T0_PREFIXES):
        return True
    if code.startswith(("5", "15", "16")):
        return _keyword_t0(name)
    return False


def t0_names(names) -> tuple:
    """其中实行 T+0 的名称"""
    return tuple(n for n in names if is_t0(n))
