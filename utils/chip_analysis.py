#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""筹码分布（成本分布）——「大家的成本都在哪」，回答「成本价多少」。

为什么需要
----------
均线告诉你趋势，但**套牢盘压在哪、平均成本多高**要靠筹码分布：股价在平均
成本上方意味着大部分持仓者浮盈（抛压小），长期压在成本下方则是套牢盘密集
（每一次反弹都有人解套卖出）。群里问「成本多少」时，这个数比均线直观。

算法（经典三角分布 + 换手率衰减）
--------------------------------
1. 把价格区间切成 ``bins`` 个等宽格子；
2. 逐日回放 K 线：老筹码按当日换手率衰减 ``chip *= (1 - 换手率)``，
   当日成交量按**三角分布**铺到当天的 [最低, 最高] 上（峰值在当日均价），
   因为一天里的成交集中在均价附近，两端少；
3. 剩下的筹码就是「还拿着的人的成本分布」。

由此得到：平均成本、获利比例（成本低于现价的筹码占比）、90% 成本区间
（5%~95% 分位）、集中度 ``(高-低)/(高+低)``、筹码峰（最密集的价位）。

口径红线
--------
* 输入只有日线 OHLCV + **当前**流通股本，与同花顺/通达信（逐笔成交 +
  历史真实换手率）**不完全等价**，但方向与量级一致；卡片上标明「近似」。
* 流通股本用「当前流通市值 / 现价」换算，历史上送转/解禁会让早年的换手率
  偏高，因此本模型对**久远筹码的权重偏低**（越早的越容易被衰减掉），
  这恰好是筹码分布想要的「近期成本更重」。
* 流通股本或成交量拿不到时返回 ``{"available": False}``，
  绝不用猜测值糊一个数出来。

纯标准库实现（不依赖 numpy/pandas），180 个格子 × 120 根 K 线只有两万次
浮点运算，毫秒级；顺带让 CI 不装 numpy 也能跑单测。
"""
from __future__ import annotations

from typing import Any, Optional

# 集中度分档：业界常用 (高-低)/(高+低)，数值越小说明筹码越挤在一个价位
CONCENTRATION_LEVELS = (
    (0.10, "高度集中"),
    (0.20, "相对集中"),
    (0.35, "偏分散"),
)
# 获利比例：越大越好（多数持仓者浮盈），与集中度是**相反**的比较方向
PROFIT_LEVELS = (
    (0.90, "几乎全部获利"),
    (0.70, "获利盘占优"),
    (0.40, "多空各半"),
    (0.15, "套牢盘占优"),
)


def concentration_level(value) -> str:
    """越小越集中（<10% 高度集中 / <20% 相对集中 / <35% 偏分散 / 其余分散）。"""
    if value is None:
        return "—"
    for limit, label in CONCENTRATION_LEVELS:
        if value < limit:
            return label
    return "分散"


def profit_level(value) -> str:
    """越大越赚（≥90% 几乎全部获利 … <20% 几乎全部套牢）。"""
    if value is None:
        return "—"
    for limit, label in PROFIT_LEVELS:
        if value >= limit:
            return label
    return "几乎全部套牢"


def _series(hist) -> list:
    """把各种形态的 hist 统一成 [(open, high, low, close, volume)]。

    接受 DataFrame（有 columns）或 list[dict]；缺列/空值返回空列表。
    """
    if hist is None:
        return []
    rows = []
    # 注意：pandas 的 Index 不能直接 bool 判断（会抛 ValueError），必须显式转 list
    try:
        cols = getattr(hist, "columns", None)
        columns = set(list(cols)) if cols is not None else set()
    except TypeError:
        columns = set()

    if columns:
        need = {"high", "low", "close"}
        if not need.issubset(columns):
            return []
        has_vol = "volume" in columns
        for rec in hist.to_dict("records"):
            rows.append(_pick(rec, has_vol))
        return [r for r in rows if r]

    for rec in hist:
        if isinstance(rec, dict):
            rows.append(_pick(rec, "volume" in rec))
    return [r for r in rows if r]


def _pick(rec: dict, has_vol: bool):
    try:
        high = float(rec["high"])
        low = float(rec["low"])
        close = float(rec["close"])
        open_ = float(rec.get("open", close))
    except (KeyError, TypeError, ValueError):
        return None
    if not (high > 0 and low > 0 and close > 0) or high < low:
        return None
    vol = 0.0
    if has_vol:
        try:
            vol = float(rec.get("volume") or 0)
        except (TypeError, ValueError):
            vol = 0.0
    return (open_, high, low, close, max(vol, 0.0))


def compute_chip_distribution(
    hist,
    float_shares: Optional[float],
    *,
    price: Optional[float] = None,
    bins: int = 180,
    decay: float = 1.0,
    min_bars: int = 5,
) -> dict[str, Any]:
    """算出筹码分布与关键读数。

    Args:
        hist: 日线数据（date/open/high/low/close/volume），越全越准，建议 ≥ 120 根。
        float_shares: 流通股本（股）。为 None/0 时返回 available=False。
        price: 现价（判断获利/套牢分界）；缺省用最后一根收盘价。
        bins: 价格格子数，180 在 120 根 K 线上足够细。
        decay: 衰减系数，1.0 = 当日换手多少就换掉多少老筹码。
        min_bars: 少于这么多根 K 线就认为样本不足。

    Returns:
        dict，available=False 时带 reason；可用时包含 avg_cost / profit_ratio /
        cost_low / cost_high / concentration / peak_price / deviation /
        centers / weights（供绘图，已归一化到 0~1）。
    """
    rows = _series(hist)
    if len(rows) < min_bars:
        return {"available": False, "reason": f"K 线不足 {min_bars} 根"}
    try:
        shares = float(float_shares or 0)
    except (TypeError, ValueError):
        shares = 0.0
    if shares <= 0:
        return {"available": False, "reason": "缺流通股本，无法还原换手率"}

    lows = [r[2] for r in rows]
    highs = [r[1] for r in rows]
    lo, hi = min(lows), max(highs)
    if not (hi > lo > 0):
        return {"available": False, "reason": "价格区间异常"}

    step = (hi - lo) / bins
    centers = [lo + (i + 0.5) * step for i in range(bins)]
    chip = [0.0] * bins
    # 逐日回放：越靠后的交易日权重越大，因为它没被后面的换手换掉
    for open_, high, low, close, vol in rows:
        turnover = 0.0 if not vol else min(max(vol / shares, 0.0), 1.0)
        keep = max(0.0, 1.0 - turnover * decay)
        if keep < 1.0:
            chip = [c * keep for c in chip]
        if vol <= 0:
            continue
        i0 = max(0, min(bins - 1, int((low - lo) / step)))
        i1 = max(i0, min(bins - 1, int((high - lo) / step)))
        peak = (open_ + high + low + close) / 4
        half = (high - low) / 2 or step
        weights = [max(0.0, 1.0 - abs(centers[i] - peak) / half) for i in range(i0, i1 + 1)]
        total_w = sum(weights)
        if total_w <= 0:  # 一字板：整根量砸在最近的一个格子里
            idx = min(bins - 1, max(0, int((close - lo) / step)))
            chip[idx] += vol
            continue
        for offset, w in enumerate(weights):
            chip[i0 + offset] += vol * w / total_w

    total = sum(chip)
    if total <= 0:
        return {"available": False, "reason": "筹码全被换手衰减掉了"}

    now_price = float(price) if price else rows[-1][3]
    avg_cost = sum(p * c for p, c in zip(centers, chip)) / total
    profit_ratio = sum(c for p, c in zip(centers, chip) if p <= now_price) / total

    # 5% / 95% 分位：累加筹码到 5% 和 95% 时对应的价格，就是 90% 成本区间
    cum = 0.0
    p05, p95 = centers[0], centers[-1]
    got05 = got95 = False
    for price_i, weight in zip(centers, chip):
        cum += weight / total
        if not got05 and cum >= 0.05:
            p05, got05 = price_i, True
        if not got95 and cum >= 0.95:
            p95, got95 = price_i, True
            break
    concentration = (p95 - p05) / (p95 + p05) if (p95 + p05) else None
    peak_price = centers[max(range(bins), key=lambda i: chip[i])]

    top = max(chip) or 1.0
    return {
        "available": True,
        "avg_cost": avg_cost,
        "price": now_price,
        "profit_ratio": profit_ratio,
        "profit_level": profit_level(profit_ratio),
        "cost_low": p05,
        "cost_high": p95,
        "concentration": concentration,
        "concentration_level": concentration_level(concentration),
        "peak_price": peak_price,
        "deviation": (now_price / avg_cost - 1) if avg_cost else None,
        "bars": len(rows),
        "float_shares": shares,
        "centers": centers,
        "weights": [c / top for c in chip],
    }


def float_shares_from_mv(float_mv: Optional[float], price: Optional[float]) -> Optional[float]:
    """流通市值（元）÷ 现价 = 流通股本（股）。任一缺失返回 None。"""
    try:
        mv = float(float_mv or 0)
        px = float(price or 0)
    except (TypeError, ValueError):
        return None
    return mv / px if mv > 0 and px > 0 else None
