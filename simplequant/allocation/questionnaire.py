"""
风险承受能力测评（参照券商投资者适当性管理的风险测评问卷）

10 道单选题，每个选项有分值；总分换算成 5 个风险等级（C1 保守型 ~ C5 进取型）。
部分选项另有等级上限：例如「不能接受任何亏损」时无论总分多少都只能是 C1。
另外记下两个硬约束，供参考配置使用：可承受的最大回撤、投资期限（年）。
"""

from dataclasses import dataclass, field, asdict
import datetime as dt

from ..i18n import L

LEVELS = {
    1: L("保守型", "Conservative"),
    2: L("稳健型", "Moderately conservative"),
    3: L("平衡型", "Balanced"),
    4: L("成长型", "Growth"),
    5: L("进取型", "Aggressive"),
}
LEVEL_DESC = {
    1: L("以保全本金为首要目标，基本不能承受本金亏损。", "Capital preservation is the primary objective; losses of principal are essentially unacceptable."),
    2: L("希望收益略高于存款，可承受小幅、短期的本金波动。", "Seeks returns modestly above deposits and can accept small, short-term fluctuations."),
    3: L("在控制风险的前提下追求较为均衡的收益，可承受一定幅度的亏损。", "Seeks balanced returns under risk control and can accept moderate losses."),
    4: L("追求较高的长期收益，可承受较大幅度的波动与阶段性亏损。", "Seeks higher long-term returns and can accept substantial volatility and interim losses."),
    5: L("追求高收益，可承受大幅波动及较大的本金亏损。", "Seeks high returns and can accept large fluctuations and significant losses of principal."),
}


def opt(text, score, cap=None, **extra):
    return {"text": text, "score": score, "cap": cap, **extra}


# 每题：key、题干、选项（text, score, 可选 cap 等级上限、max_dd / years 硬约束）
QUESTIONS = [
    {"key": "age", "text": L("您的年龄处于以下哪个区间？", "Which age range are you in?"), "options": [
        opt(L("18~30 岁", "18-30"), 3),
        opt(L("31~45 岁", "31-45"), 4),
        opt(L("46~55 岁", "46-55"), 3),
        opt(L("56~65 岁", "56-65"), 2),
        opt(L("65 岁以上", "Over 65"), 1),
    ]},
    {"key": "income", "text": L("您的家庭年收入约为？", "What is your approximate annual household income?"), "options": [
        opt(L("10 万元以下", "Below CNY 100,000"), 1),
        opt(L("10 万 ~ 50 万元", "CNY 100,000-500,000"), 2),
        opt(L("50 万 ~ 100 万元", "CNY 500,000-1,000,000"), 3),
        opt(L("100 万元以上", "Above CNY 1,000,000"), 4),
    ]},
    {"key": "share", "text": L("拟投资金额占您家庭金融资产的比例约为？",
                               "What share of your household financial assets do you plan to invest?"), "options": [
        opt(L("70% 以上", "Above 70%"), 1),
        opt(L("50% ~ 70%", "50%-70%"), 2),
        opt(L("30% ~ 50%", "30%-50%"), 3),
        opt(L("10% ~ 30%", "10%-30%"), 4),
        opt(L("10% 以下", "Below 10%"), 5),
    ]},
    {"key": "stability", "text": L("您的收入来源及其稳定性如何？", "How stable is your source of income?"), "options": [
        opt(L("无固定收入", "No regular income"), 1),
        opt(L("有收入，但不稳定", "Income, but irregular"), 2),
        opt(L("收入稳定", "Stable income"), 3),
        opt(L("收入稳定且持续增长", "Stable and growing income"), 4),
    ]},
    {"key": "experience", "text": L("您投资股票、基金、债券等金融产品的经验有多长时间？",
                                    "How long have you invested in stocks, funds, bonds or similar products?"), "options": [
        opt(L("无投资经验", "No experience"), 1),
        opt(L("2 年以下", "Less than 2 years"), 2),
        opt(L("2 ~ 5 年", "2-5 years"), 3),
        opt(L("5 ~ 10 年", "5-10 years"), 4),
        opt(L("10 年以上", "More than 10 years"), 5),
    ]},
    {"key": "products", "text": L("您曾投资过的风险最高的品种是？", "What is the riskiest product you have invested in?"), "options": [
        opt(L("银行存款、货币基金", "Bank deposits or money market funds"), 1),
        opt(L("债券、债券基金、银行理财", "Bonds, bond funds or bank wealth products"), 2),
        opt(L("股票、混合型或股票型基金", "Stocks, balanced or equity funds"), 3),
        opt(L("期货、期权、融资融券等", "Futures, options or margin trading"), 4),
    ]},
    {"key": "horizon", "text": L("您计划的投资期限为？", "What is your planned investment horizon?"), "options": [
        opt(L("1 年以内", "Less than 1 year"), 1, cap=2, years=1),
        opt(L("1 ~ 3 年", "1-3 years"), 2, years=3),
        opt(L("3 ~ 5 年", "3-5 years"), 3, years=5),
        opt(L("5 年以上", "More than 5 years"), 4, years=10),
    ]},
    {"key": "goal", "text": L("您的投资目标是？", "What is your investment objective?"), "options": [
        opt(L("保全本金，不希望出现亏损", "Preserve capital and avoid any loss"), 1),
        opt(L("获得略高于存款的稳定收益", "Stable returns slightly above deposits"), 2),
        opt(L("获得较高收益，可承受一定波动", "Higher returns with some volatility"), 3),
        opt(L("追求高收益，可承受较大波动", "High returns with substantial volatility"), 4),
    ]},
    {"key": "loss", "text": L("您能够承受的最大投资亏损（本金的回撤幅度）为？",
                              "What is the largest loss (drawdown of principal) you can tolerate?"), "options": [
        opt(L("不能接受任何亏损", "No loss at all"), 1, cap=1, max_dd=0.03),
        opt(L("10% 以内", "Up to 10%"), 2, max_dd=0.10),
        opt(L("10% ~ 20%", "10%-20%"), 3, max_dd=0.20),
        opt(L("20% ~ 35%", "20%-35%"), 4, max_dd=0.35),
        opt(L("35% 以上", "More than 35%"), 5, max_dd=0.50),
    ]},
    {"key": "reaction", "text": L("若您的投资在一年内下跌 20%，您将如何处理？",
                                  "If your investment fell 20% within a year, what would you do?"), "options": [
        opt(L("全部卖出", "Sell everything"), 1),
        opt(L("卖出部分，降低风险", "Sell part of it to reduce risk"), 2),
        opt(L("继续持有，等待回升", "Hold and wait for a recovery"), 3),
        opt(L("逢低加仓", "Buy more"), 4),
    ]},
]

MIN_SCORE = sum(min(o["score"] for o in q["options"]) for q in QUESTIONS)
MAX_SCORE = sum(max(o["score"] for o in q["options"]) for q in QUESTIONS)
LEVEL_CUTS = (0.2, 0.4, 0.6, 0.8)     # 得分在 [最低分, 最高分] 中的相对位置 → 等级


@dataclass
class Profile:
    answers: dict                 # {题目 key: 选项序号}
    score: int
    level: int                    # 1~5
    score_level: int              # 仅按总分得到的等级（与 level 不同时说明触发了等级上限）
    max_dd: float                 # 可承受的最大回撤（正数，如 0.2）
    years: int                    # 投资期限（年）
    time: str = field(default_factory=lambda: dt.datetime.now().strftime("%Y-%m-%d %H:%M"))

    def to_dict(self) -> dict:
        return asdict(self)


def evaluate(answers: dict) -> Profile:
    """answers: {题目 key: 选项序号}；必须每题都答"""
    missing = [q["key"] for q in QUESTIONS if q["key"] not in answers]
    if missing:
        raise ValueError(f"unanswered questions / 尚有未作答的题目: {missing}")
    score, cap, max_dd, years = 0, 5, 0.2, 5
    for q in QUESTIONS:
        o = q["options"][int(answers[q["key"]])]
        score += o["score"]
        if o.get("cap"):
            cap = min(cap, o["cap"])
        max_dd = o.get("max_dd", max_dd)
        years = o.get("years", years)
    rel = (score - MIN_SCORE) / (MAX_SCORE - MIN_SCORE)
    score_level = 1 + sum(rel >= c for c in LEVEL_CUTS)
    return Profile(answers={k: int(v) for k, v in answers.items()}, score=score, level=min(score_level, cap),
                   score_level=score_level, max_dd=max_dd, years=years)


def preset_profile(level: int) -> Profile:
    """未测评时直接指定等级（例如论文实验中的模拟用户）：取该等级典型的硬约束"""
    max_dd = {1: 0.03, 2: 0.10, 3: 0.20, 4: 0.35, 5: 0.50}[level]
    return Profile(answers={}, score=0, level=level, score_level=level, max_dd=max_dd, years=5)
