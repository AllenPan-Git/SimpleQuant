"""
机器学习合成因子：用过去的「因子 → 未来收益排名」训练模型，模型的预测值就是综合得分（只依赖 numpy）

与 IC / ICIR 加权一样，只替换「把多个因子合成一个得分」这一步，后面的选前 N 只、回测、可信度检查都不变。

数据：
- 特征 = 所选因子按日去极值、标准化（及中性化）后的值（factors.factor_zscores，与其他加权方式相同）
- 目标 = 未来 h 日收益（research.forward_returns：T+1 开盘买入、T+1+h 开盘卖出）在当天可选股票里的排名，
  换算到 -0.5 ~ 0.5。用排名而不是收益本身：不受大盘涨跌影响，个别暴涨暴跌的股票也不会主导训练
防前视：调仓日 T 只用 d ≤ T − (h+1) 的样本（d 的目标用到 d+1+h 日开盘价，T 日收盘时已知），与 ic_weights 相同
训练：滚动窗口（默认 3 年）；样本每隔 h 天取一天，相邻样本的持有期不重叠，避免同一段行情被重复计入；
      最多每 RETRAIN_DAYS 个交易日重训一次（每周调仓时沿用最近一次的模型）；样本不足时该期回退到手动权重

四种模型（都在本文件里用 numpy 实现，固定随机种子，结果可复现）：
- ridge 岭回归：线性模型，系数向 0 收缩，相关因子多时更稳
- knn K 近邻：找特征最接近的 K 个历史样本，取它们目标值的平均
- rf 随机森林：许多棵深度有限的回归树取平均，每棵树用有放回抽样的样本、每层随机挑一部分因子
- gbdt 梯度提升：浅树逐棵拟合前面的残差，乘学习率后累加
回归树用直方图算法：每个因子按训练样本的分位数分成最多 BINS 个箱，按层生长，
每层用 np.bincount 一次算出所有节点、所有切分点的增益。
"""

import numpy as np
import pandas as pd

from ..i18n import L

MODELS = {"ridge": L("岭回归", "Ridge regression"), "knn": L("K 近邻", "K-nearest neighbors"),
          "rf": L("随机森林", "Random forest"), "gbdt": L("梯度提升树", "Gradient boosting")}
# 各模型的参数与默认值；window = 训练窗口（交易日）
DEFAULTS = {"ridge": {"alpha": 1.0}, "knn": {"k": 100}, "rf": {"trees": 100, "depth": 5},
            "gbdt": {"trees": 100, "depth": 3, "lr": 0.05}}
DEFAULT_MODEL = "rf"
DEFAULT_WINDOW = 756
PARAM_LABELS = {"window": L("训练窗口（交易日）", "Training window (days)"), "alpha": L("正则强度", "Regularization"),
                "k": L("近邻数 K", "Neighbors K"), "trees": L("树的数量", "Trees"), "depth": L("树的深度", "Tree depth"),
                "lr": L("学习率", "Learning rate")}
# 参数的范围：(最小, 最大, 是否整数)
PARAM_RANGES = {"window": (120, 2520, True), "alpha": (0.0, 100.0, False), "k": (1, 2000, True),
                "trees": (1, 1000, True), "depth": (1, 10, True), "lr": (0.001, 1.0, False)}
RETRAIN_DAYS = 20        # 最多每这么多个交易日重训一次
MIN_DAYS = 8             # 训练至少要有这么多个样本日
MIN_ROWS = 200           # 以及这么多行样本
MIN_NAMES = 10           # 当天样本少于这么多只股票的不用
BINS = 32
SEED = 0


def config(spec: dict) -> dict:
    """策略描述里的 ml 设置补上默认值：{"model", "window", 以及该模型的参数}"""
    raw = spec.get("ml") or {}
    model = raw.get("model") if raw.get("model") in MODELS else DEFAULT_MODEL
    out = {"model": model, "window": int(raw.get("window", DEFAULT_WINDOW))}
    for k, v in DEFAULTS[model].items():
        out[k] = type(v)(raw.get(k, v))
    return out


# ================= 模型 =================
class Ridge:
    def __init__(self, alpha=1.0, **_):
        self.alpha = float(alpha)

    def fit(self, X, y):
        """系数的惩罚 = alpha × 样本数（特征已标准化，alpha = 1 大约把系数向 0 收缩一半）；截距不惩罚"""
        self.mx, self.my = X.mean(0), y.mean()
        Xc = X - self.mx
        A = Xc.T @ Xc + (self.alpha * len(X) + 1e-9) * np.eye(X.shape[1])
        self.coef = np.linalg.solve(A, Xc.T @ (y - self.my))
        return self

    def predict(self, X):
        return (X - self.mx) @ self.coef + self.my

    def importance(self):
        a = np.abs(self.coef)
        return a / a.sum() if a.sum() > 0 else a


class KNN:
    CHUNK = 256

    def __init__(self, k=100, **_):
        self.k = int(k)

    def fit(self, X, y):
        self.X, self.y = X, y
        self.x2 = (X ** 2).sum(1)
        return self

    def predict(self, Q):
        k = min(self.k, len(self.X))
        out = np.empty(len(Q))
        for a in range(0, len(Q), self.CHUNK):
            q = Q[a:a + self.CHUNK]
            d = (q ** 2).sum(1)[:, None] + self.x2[None, :] - 2 * q @ self.X.T
            idx = np.argpartition(d, k - 1, axis=1)[:, :k] if k < len(self.X) else \
                np.broadcast_to(np.arange(len(self.X)), (len(q), len(self.X)))
            out[a:a + len(q)] = self.y[idx].mean(1)
        return out

    def importance(self):
        return None


class Binner:
    """每个特征按分位数切成最多 BINS 个箱：箱号 = 不大于该值的切点个数"""

    def __init__(self, X, bins=BINS):
        qs = np.linspace(0, 1, bins + 1)[1:-1]
        self.edges = [np.unique(np.quantile(X[:, j], qs)) for j in range(X.shape[1])]
        self.n_bins = max(len(e) for e in self.edges) + 1

    def transform(self, X):
        return np.column_stack([np.searchsorted(e, X[:, j], side="right") for j, e in enumerate(self.edges)]
                               ).astype(np.int64)


def grow_tree(B, g, w, n_bins, depth, min_leaf, lam=0.0, n_feat=None, rng=None):
    """
    按层生长一棵回归树（平方损失）。
    B：箱号矩阵（样本 × 特征）；g：要拟合的值；w：样本权重（有放回抽样的次数，0 = 未抽中）
    叶子值 = Σwg / (Σw + lam)；切分增益 = GL²/(NL+lam) + GR²/(NR+lam) − G²/(N+lam)
    n_feat：每层随机挑这么多个特征比较（随机森林）；None 为全部
    返回 (树, 各特征的增益合计)。树是 dict：feat、thr（箱号 ≤ thr 走左边）、left、right、value，叶子的 feat = -1
    """
    n, d = B.shape
    wg = w * g
    feat, thr, left, right = [-1], [0], [-1], [-1]
    value = [float(wg.sum() / (w.sum() + lam)) if w.sum() + lam > 0 else 0.0]
    gains = np.zeros(d)
    node = np.zeros(n, dtype=np.int64)
    frontier = [0]
    for _ in range(depth):
        if not frontier:
            break
        m = len(frontier)
        pos = np.full(len(feat), -1)
        pos[frontier] = np.arange(m)
        p = pos[node]
        act = (p >= 0) & (w > 0)
        pa, wa, wga = p[act], w[act], wg[act]
        Gt = np.bincount(pa, weights=wga, minlength=m)
        Nt = np.bincount(pa, weights=wa, minlength=m)
        parent = Gt ** 2 / (Nt + lam + 1e-12)
        best = np.zeros(m)
        bf = np.full(m, -1)
        bs = np.zeros(m, dtype=np.int64)
        bl = np.zeros((m, 2))                       # 左子节点的 (G, N)
        feats = np.arange(d) if n_feat is None or n_feat >= d else np.sort(rng.choice(d, n_feat, replace=False))
        for j in feats:
            idx = pa * n_bins + B[act, j]
            G = np.bincount(idx, weights=wga, minlength=m * n_bins).reshape(m, n_bins).cumsum(1)[:, :-1]
            N = np.bincount(idx, weights=wa, minlength=m * n_bins).reshape(m, n_bins).cumsum(1)[:, :-1]
            GR, NR = Gt[:, None] - G, Nt[:, None] - N
            gain = G ** 2 / (N + lam + 1e-12) + GR ** 2 / (NR + lam + 1e-12) - parent[:, None]
            gain[(N < min_leaf) | (NR < min_leaf)] = -np.inf
            s = gain.argmax(1)
            gb = gain[np.arange(m), s]
            up = gb > best + 1e-12
            best[up], bf[up], bs[up] = gb[up], j, s[up]
            bl[up, 0], bl[up, 1] = G[up, s[up]], N[up, s[up]]
        nxt = []
        for i, nid in enumerate(frontier):
            if bf[i] < 0:
                continue
            gl, nl = bl[i]
            gr, nr = Gt[i] - gl, Nt[i] - nl
            feat[nid], thr[nid] = int(bf[i]), int(bs[i])
            gains[bf[i]] += best[i]
            for gg, nn in ((gl, nl), (gr, nr)):
                feat.append(-1), thr.append(0), left.append(-1), right.append(-1)
                value.append(float(gg / (nn + lam)) if nn + lam > 0 else 0.0)
                nxt.append(len(feat) - 1)
            left[nid], right[nid] = nxt[-2], nxt[-1]
        if not nxt:
            break
        tree = _arrays(feat, thr, left, right, value)
        node = _step(tree, B, node)
        frontier = nxt
    return _arrays(feat, thr, left, right, value), gains


def _arrays(feat, thr, left, right, value):
    return {"feat": np.array(feat), "thr": np.array(thr), "left": np.array(left), "right": np.array(right),
            "value": np.array(value)}


def _step(tree, B, node):
    """所在节点不是叶子的样本往下走一层"""
    f = tree["feat"][node]
    inner = f >= 0
    if inner.any():
        rows = np.flatnonzero(inner)
        go_left = B[rows, f[inner]] <= tree["thr"][node[inner]]
        node = node.copy()
        node[rows] = np.where(go_left, tree["left"][node[inner]], tree["right"][node[inner]])
    return node


def predict_tree(tree, B, depth):
    node = np.zeros(len(B), dtype=np.int64)
    for _ in range(depth):
        node = _step(tree, B, node)
    return tree["value"][node]


class _Trees:
    def __init__(self, trees=100, depth=4, seed=SEED, **_):
        self.n_trees, self.depth, self.seed = int(trees), int(depth), seed

    def _min_leaf(self, n):
        return max(20, n // 200)

    def importance(self):
        s = self.gains.sum()
        return self.gains / s if s > 0 else self.gains

    def predict(self, X):
        B = self.binner.transform(X)
        return self.base + sum(self.scale * predict_tree(t, B, self.depth) for t in self.trees)


class RandomForest(_Trees):
    def __init__(self, trees=100, depth=5, **kw):
        super().__init__(trees, depth, **kw)

    def fit(self, X, y):
        rng = np.random.default_rng(self.seed)
        self.binner = Binner(X)
        B = self.binner.transform(X)
        n, d = B.shape
        n_feat = max(1, int(np.ceil(d * 0.6)))
        self.trees, self.gains = [], np.zeros(d)
        self.base, self.scale = 0.0, 1.0 / self.n_trees
        for _ in range(self.n_trees):
            w = np.bincount(rng.integers(0, n, n), minlength=n).astype(float)    # 有放回抽样
            t, g = grow_tree(B, y, w, self.binner.n_bins, self.depth, self._min_leaf(n), 0.0, n_feat, rng)
            self.trees.append(t)
            self.gains += g
        return self


class GradientBoosting(_Trees):
    SUBSAMPLE = 0.8
    LAMBDA = 1.0

    def __init__(self, trees=100, depth=3, lr=0.05, **kw):
        super().__init__(trees, depth, **kw)
        self.lr = float(lr)

    def fit(self, X, y):
        rng = np.random.default_rng(self.seed)
        self.binner = Binner(X)
        B = self.binner.transform(X)
        n, d = B.shape
        self.trees, self.gains = [], np.zeros(d)
        self.base, self.scale = float(y.mean()), self.lr
        f = np.full(n, self.base)
        for _ in range(self.n_trees):
            w = (rng.random(n) < self.SUBSAMPLE).astype(float)
            t, g = grow_tree(B, y - f, w, self.binner.n_bins, self.depth, self._min_leaf(n), self.LAMBDA)
            self.trees.append(t)
            self.gains += g
            f += self.lr * predict_tree(t, B, self.depth)
        return self


def make_model(cfg: dict):
    cls = {"ridge": Ridge, "knn": KNN, "rf": RandomForest, "gbdt": GradientBoosting}[cfg["model"]]
    return cls(**{k: v for k, v in cfg.items() if k not in ("model", "window")})


# ================= 训练与打分 =================
def _rank_target(fwd_row: np.ndarray, ok: np.ndarray) -> np.ndarray:
    """当天可用股票的未来收益排名 → -0.5 ~ 0.5（并列取平均名次）"""
    r = pd.Series(fwd_row[ok]).rank().to_numpy()
    return (r - 1) / max(len(r) - 1, 1) - 0.5


def _rank_corr(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < MIN_NAMES:
        return np.nan
    ra, rb = pd.Series(a).rank().to_numpy(), pd.Series(b).rank().to_numpy()
    ra, rb = ra - ra.mean(), rb - rb.mean()
    den = np.sqrt((ra ** 2).sum() * (rb ** 2).sum())
    return float((ra * rb).sum() / den) if den > 0 else np.nan


def ml_scores(panel, spec: dict, mask: pd.DataFrame, dates: pd.DatetimeIndex, horizon: int,
              cache: dict | None = None) -> tuple[pd.DataFrame, dict]:
    """
    每个调仓日的综合得分（行 = dates，列 = 股票）与训练信息。
    训练信息：model、trainings（每次训练的日期、样本日数、样本行数、训练集 IC）、importance（因子 → 平均占比，
    K 近邻为 None）、fallback（样本不足、按手动权重打分的调仓日）
    """
    from .factors import composite, factor_zscores
    from .research import forward_returns
    cache = {} if cache is None else cache
    cfg = config(spec)
    neutral = spec.get("neutralize") or {}
    nkey = (bool(neutral.get("industry")), bool(neutral.get("size")))
    keys = [f["key"] for f in spec["factors"]]
    ckey = ("ml", tuple(keys), nkey, horizon, tuple(sorted(cfg.items())), tuple(dates))
    if ckey in cache:
        return cache[ckey]

    def cached(key, fn):
        if key not in cache:
            cache[key] = fn()
        return cache[key]

    zs = [cached(("z", k, nkey), lambda k=k: factor_zscores(panel, k, mask, neutral)) for k in keys]
    fwd = cached(("fwd", horizon), lambda: forward_returns(panel, horizon))
    cal = panel.calendar
    codes = zs[0].columns
    Z = np.stack([z.reindex(index=cal, columns=codes).to_numpy(dtype=float) for z in zs], axis=-1)   # T × N × F
    feat_ok = np.isfinite(Z).all(-1) & mask.reindex(index=cal, columns=codes).fillna(False).to_numpy(bool)
    F = fwd.reindex(index=cal, columns=codes).to_numpy(dtype=float)
    lab_ok = feat_ok & np.isfinite(F)

    out = pd.DataFrame(np.nan, index=dates, columns=codes)
    trainings, imps, fallback = [], [], []
    model, trained_at = None, None
    manual = None
    for T in dates:
        iT = cal.get_loc(T)
        if model is None or iT - trained_at >= RETRAIN_DAYS:
            end = iT - (horizon + 1)
            days = [i for i in range(end, max(end - cfg["window"], -1), -horizon)
                    if i >= 0 and lab_ok[i].sum() >= MIN_NAMES][::-1]
            rows = sum(int(lab_ok[i].sum()) for i in days)
            if len(days) >= MIN_DAYS and rows >= MIN_ROWS:
                X = np.concatenate([Z[i][lab_ok[i]] for i in days])
                y = np.concatenate([_rank_target(F[i], lab_ok[i]) for i in days])
                model = make_model(cfg).fit(X, y)
                trained_at = iT
                recent = days[-12:]                     # 训练集 IC：最近 12 个样本日上，模型对训练样本的拟合程度
                pred = model.predict(np.concatenate([Z[i][lab_ok[i]] for i in recent]))
                ics, a = [], 0
                for i in recent:
                    n = int(lab_ok[i].sum())
                    ics.append(_rank_corr(pred[a:a + n], F[i][lab_ok[i]]))
                    a += n
                trainings.append({"date": T, "days": len(days), "rows": rows, "train_ic": float(np.nanmean(ics))})
                imp = model.importance()
                if imp is not None:
                    imps.append(imp)
            else:
                model = None
        ok = feat_ok[iT]
        if model is not None and ok.any():
            out.loc[T, codes[ok]] = model.predict(Z[iT][ok])
        else:
            if manual is None:
                manual = composite(panel, spec["factors"], mask, "manual", neutral, cache=cache)
            out.loc[T] = manual.loc[T].reindex(codes)
            fallback.append(T)
    info = {"model": cfg["model"], "config": cfg, "trainings": trainings, "fallback": fallback,
            "importance": dict(zip(keys, np.mean(imps, axis=0).tolist())) if imps else None}
    cache[ckey] = (out, info)
    return out, info


def oos_ic(panel, spec: dict, mask: pd.DataFrame, score: pd.DataFrame, horizon: int, fallback=(),
           cache: dict | None = None) -> pd.DataFrame:
    """
    样本外 IC 对比（各调仓日 T 的得分与 T 之后实际的 h 日收益的秩相关；回退到手动权重的日期不计）：
    机器学习、ICIR 合成（同样的因子，回看期用 ic_lookback）、各因子按所设方向单独使用。
    返回：行 = 调仓日，列 = "ml"、"icir"、各因子 key
    """
    from .factors import composite, FACTORS
    from .research import forward_returns, rank_ic
    cache = {} if cache is None else cache
    neutral = spec.get("neutralize") or {}
    nkey = (bool(neutral.get("industry")), bool(neutral.get("size")))
    dates = score.index.difference(pd.DatetimeIndex(list(fallback)))
    fwd = cache.setdefault(("fwd", horizon), forward_returns(panel, horizon)) if ("fwd", horizon) not in cache \
        else cache[("fwd", horizon)]
    r = fwd.loc[dates]
    out = {"ml": rank_ic(score.loc[dates], r)}
    icir = composite(panel, spec["factors"], mask, "icir", neutral, horizon=horizon,
                     lookback=int(spec.get("ic_lookback", 252)), cache=cache)
    out["icir"] = rank_ic(icir.loc[dates], r)
    for f in spec["factors"]:
        z = cache.get(("z", f["key"], nkey))
        if z is None:
            continue
        d = f.get("direction", FACTORS.get(f["key"], {}).get("direction", 1))
        out[f["key"]] = rank_ic(z.loc[dates] * d, r)
    return pd.DataFrame(out)


def summary(info: dict, ic: pd.DataFrame) -> dict:
    """结果页与可信度检查用：训练集 IC 均值、各列样本外 IC 均值与期数"""
    tr = [t["train_ic"] for t in info["trainings"] if np.isfinite(t["train_ic"])]
    oos = ic.mean().to_dict() if len(ic) else {}
    return {"train_ic": float(np.mean(tr)) if tr else np.nan, "oos": oos,
            "n_oos": int(ic["ml"].notna().sum()) if "ml" in ic else 0,
            "n_train": len(info["trainings"]), "n_fallback": len(info["fallback"])}
