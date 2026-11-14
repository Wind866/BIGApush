#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
个股策略分析与买入建议合成。

输入：stock_analyzer.analyze() 的结果（技术面 / 资金流向 / 基本面 / 结论）。
输出：
  - strategy_analysis: 各维度策略信号列表 [{name, signal(看多/看空/中性), note}]
  - buy_advice:       综合买入建议 {action, score, rating, rationale[], risks[], disclaimer}
"""
from typing import Any, Dict, List


def _signal(level: int) -> str:
    """把数值化的多空强度映射成文字信号。>0 看多，<0 看空，=0 中性。"""
    if level > 0:
        return "看多"
    if level < 0:
        return "看空"
    return "中性"


def build_strategy_analysis(analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从 analyze() 结果里抽取若干可解释的策略信号。"""
    technical = analysis.get("technical") or {}
    indicators = technical.get("indicators") or {}
    fund_flow = analysis.get("fund_flow") or {}
    fundamental = analysis.get("fundamental") or {}

    flow = fund_flow.get("flow_analysis") or {}
    direction = flow.get("direction", "未知")          # 流入 / 流出 / 平衡
    main_inflow = flow.get("main_inflow") or 0

    val = fundamental.get("valuation") or {}
    fin = fundamental.get("financial") or {}
    gr = fundamental.get("growth") or {}

    pe_level = val.get("pe_level", "未知")
    roe = fin.get("roe") or 0
    debt_level = fin.get("debt_level", "未知")
    profit_growth = gr.get("profit_growth")

    trend = technical.get("trend", "未知")              # 上升趋势 / 下降趋势 / 横盘
    macd = indicators.get("MACD", "未知")
    kdj = indicators.get("KDJ", "未知")

    signals: List[Dict[str, Any]] = []

    # 1. 趋势跟踪策略
    if trend == "上升趋势":
        signals.append({"name": "趋势跟踪", "signal": "看多", "note": "均线多头排列，中期趋势向上"})
    elif trend == "下降趋势":
        signals.append({"name": "趋势跟踪", "signal": "看空", "note": "均线空头排列，中期趋势向下"})
    else:
        signals.append({"name": "趋势跟踪", "signal": "中性", "note": "价格横盘整理，方向不明"})

    # 2. 动能策略（MACD + KDJ 合成）
    momentum_score = 0
    if macd in ("多头", "金叉"):
        momentum_score += 1
    elif macd in ("空头", "死叉"):
        momentum_score -= 1
    if kdj == "金叉":
        momentum_score += 1
    elif kdj == "死叉":
        momentum_score -= 1
    elif kdj == "超卖":
        momentum_score += 1
    elif kdj == "超买":
        momentum_score -= 1
    note = f"MACD={macd}, KDJ={kdj}"
    signals.append({"name": "动能策略", "signal": _signal(momentum_score), "note": note})

    # 3. 资金面策略
    if direction == "流入" or main_inflow > 0:
        signals.append({"name": "资金面", "signal": "看多", "note": f"主力净流入 {_fmt_money(main_inflow)}，资金吸纳"})
    elif direction == "流出" or main_inflow < 0:
        signals.append({"name": "资金面", "signal": "看空", "note": f"主力净流出 {_fmt_money(main_inflow)}，资金撤离"})
    else:
        signals.append({"name": "资金面", "signal": "中性", "note": "主力资金进出平衡"})

    # 4. 估值策略
    if pe_level == "低":
        signals.append({"name": "估值策略", "signal": "看多", "note": f"PE 处于低位（{val.get('pe')}），估值便宜"})
    elif pe_level == "高":
        signals.append({"name": "估值策略", "signal": "看空", "note": f"PE 偏高（{val.get('pe')}），注意泡沫"})
    else:
        signals.append({"name": "估值策略", "signal": "中性", "note": f"PE 中性（{val.get('pe')}）"})

    # 5. 成长策略
    if profit_growth is not None:
        if profit_growth > 0.15:
            signals.append({"name": "成长策略", "signal": "看多", "note": f"净利润同比 +{profit_growth*100:.1f}%，高成长"})
        elif profit_growth > 0:
            signals.append({"name": "成长策略", "signal": "中性", "note": f"净利润同比 +{profit_growth*100:.1f}%，温和增长"})
        else:
            signals.append({"name": "成长策略", "signal": "看空", "note": f"净利润同比 {profit_growth*100:.1f}%，业绩承压"})
    else:
        signals.append({"name": "成长策略", "signal": "中性", "note": "无成长数据"})

    # 6. 质量策略（ROE）
    if roe and roe > 15:
        signals.append({"name": "质量策略", "signal": "看多", "note": f"ROE={roe:.1f}%，盈利能力强"})
    elif roe and roe < 8:
        signals.append({"name": "质量策略", "signal": "看空", "note": f"ROE={roe:.1f}%，盈利能力弱"})
    else:
        signals.append({"name": "质量策略", "signal": "中性", "note": f"ROE={roe if roe else '未知'}"})

    # 7. 安全策略（负债率）
    if debt_level == "高":
        signals.append({"name": "安全策略", "signal": "看空", "note": "资产负债率偏高，财务杠杆大"})
    elif debt_level == "低":
        signals.append({"name": "安全策略", "signal": "看多", "note": "资产负债率低，财务稳健"})
    else:
        signals.append({"name": "安全策略", "signal": "中性", "note": "负债率中性"})

    return signals


def build_buy_advice(analysis: Dict[str, Any], real_val: Dict[str, Any] = None,
                     real_fin: Dict[str, Any] = None) -> Dict[str, Any]:
    """综合技术面、资金面、估值、成长、质量，给出买入建议。"""
    real_val = real_val or {}
    real_fin = real_fin or {}

    conclusion = analysis.get("conclusion") or {}
    rating = conclusion.get("rating", "中性")
    tech = analysis.get("technical") or {}
    indicators = tech.get("indicators") or {}
    fund_flow = analysis.get("fund_flow") or {}
    flow = fund_flow.get("flow_analysis") or {}
    main_inflow = flow.get("main_inflow") or 0
    fundamental = analysis.get("fundamental") or {}
    val = fundamental.get("valuation") or {}
    fin = fundamental.get("financial") or {}
    gr = fundamental.get("growth") or {}

    # 评分（0~100），基准 50
    score = 50

    rating_delta = {
        "强烈看多": 25, "看多": 15, "中性": 0, "看空": -15, "强烈看空": -25,
    }.get(rating, 0)
    score += rating_delta

    if main_inflow > 0:
        score += 10
    elif main_inflow < 0:
        score -= 10

    pe_level = val.get("pe_level") or real_val.get("pe_level")
    pe = val.get("pe") or real_val.get("pe")
    if pe_level == "低" or (pe is not None and pe < 20):
        score += 10
    elif pe_level == "高" or (pe is not None and pe >= 40):
        score -= 10

    profit_growth = gr.get("profit_growth")
    if profit_growth is not None:
        if profit_growth > 0.15:
            score += 10
        elif profit_growth > 0:
            score += 5
        else:
            score -= 10

    roe = fin.get("roe")
    if roe is not None:
        if roe > 15:
            score += 5
        elif roe < 8:
            score -= 5

    debt_level = fin.get("debt_level")
    if debt_level == "高":
        score -= 5

    score = max(0, min(100, score))

    if score >= 75:
        action = "强烈买入"
    elif score >= 60:
        action = "买入"
    elif score >= 45:
        action = "观望"
    elif score >= 30:
        action = "减仓"
    else:
        action = "卖出"

    # 理由：取看多与看空的策略信号
    signals = build_strategy_analysis_from_parts(
        trend=tech.get("trend"), macd=indicators.get("MACD"), kdj=indicators.get("KDJ"),
        direction=flow.get("direction"), main_inflow=main_inflow,
        pe_level=pe_level, pe=pe, roe=roe, debt_level=debt_level,
        profit_growth=profit_growth,
    )
    rationale, bull, bear = [], [], []
    for s in signals:
        if s["signal"] == "看多":
            bull.append(f"{s['name']}：{s['note']}")
        elif s["signal"] == "看空":
            bear.append(f"{s['name']}：{s['note']}")
    if bull:
        rationale.append("看多因素：" + "；".join(bull))
    if bear:
        rationale.append("看空因素：" + "；".join(bear))
    if not rationale:
        rationale.append("多空因素大致均衡，建议等待更明确信号")

    risks = []
    base_risk = conclusion.get("risk")
    if base_risk:
        risks.append(base_risk)
    risks.append("模型基于公开历史数据，未考虑突发消息与系统性风险")
    risks.append("仅供研究参考，不构成任何投资建议")

    return {
        "action": action,
        "score": score,
        "rating": rating,
        "bull_count": len(bull),
        "bear_count": len(bear),
        "rationale": rationale,
        "risks": risks,
        "disclaimer": "以上为量化模型基于公开数据的参考结论，不构成投资建议。",
    }


def build_strategy_analysis_from_parts(trend=None, macd=None, kdj=None, direction=None,
                                       main_inflow=0, pe_level=None, pe=None, roe=None,
                                       debt_level=None, profit_growth=None) -> List[Dict[str, Any]]:
    """与 build_strategy_analysis 同口径，但从已拆出的字段构造（供 buy_advice 复用）。"""
    signals: List[Dict[str, Any]] = []
    if trend == "上升趋势":
        signals.append({"name": "趋势跟踪", "signal": "看多", "note": "均线多头排列，中期趋势向上"})
    elif trend == "下降趋势":
        signals.append({"name": "趋势跟踪", "signal": "看空", "note": "均线空头排列，中期趋势向下"})
    else:
        signals.append({"name": "趋势跟踪", "signal": "中性", "note": "价格横盘整理，方向不明"})

    momentum = 0
    if macd in ("多头", "金叉"):
        momentum += 1
    elif macd in ("空头", "死叉"):
        momentum -= 1
    if kdj == "金叉":
        momentum += 1
    elif kdj == "死叉":
        momentum -= 1
    elif kdj == "超卖":
        momentum += 1
    elif kdj == "超买":
        momentum -= 1
    signals.append({"name": "动能策略", "signal": _signal(momentum), "note": f"MACD={macd}, KDJ={kdj}"})

    if direction == "流入" or main_inflow > 0:
        signals.append({"name": "资金面", "signal": "看多", "note": f"主力净流入 {_fmt_money(main_inflow)}，资金吸纳"})
    elif direction == "流出" or main_inflow < 0:
        signals.append({"name": "资金面", "signal": "看空", "note": f"主力净流出 {_fmt_money(main_inflow)}，资金撤离"})
    else:
        signals.append({"name": "资金面", "signal": "中性", "note": "主力资金进出平衡"})

    if pe_level == "低":
        signals.append({"name": "估值策略", "signal": "看多", "note": f"PE 处于低位（{pe}），估值便宜"})
    elif pe_level == "高":
        signals.append({"name": "估值策略", "signal": "看空", "note": f"PE 偏高（{pe}），注意泡沫"})
    else:
        signals.append({"name": "估值策略", "signal": "中性", "note": f"PE 中性（{pe}）"})

    if profit_growth is not None:
        if profit_growth > 0.15:
            signals.append({"name": "成长策略", "signal": "看多", "note": f"净利润同比 +{profit_growth*100:.1f}%，高成长"})
        elif profit_growth > 0:
            signals.append({"name": "成长策略", "signal": "中性", "note": f"净利润同比 +{profit_growth*100:.1f}%，温和增长"})
        else:
            signals.append({"name": "成长策略", "signal": "看空", "note": f"净利润同比 {profit_growth*100:.1f}%，业绩承压"})
    else:
        signals.append({"name": "成长策略", "signal": "中性", "note": "无成长数据"})

    if roe and roe > 15:
        signals.append({"name": "质量策略", "signal": "看多", "note": f"ROE={roe:.1f}%，盈利能力强"})
    elif roe and roe < 8:
        signals.append({"name": "质量策略", "signal": "看空", "note": f"ROE={roe:.1f}%，盈利能力弱"})
    else:
        signals.append({"name": "质量策略", "signal": "中性", "note": f"ROE={roe if roe else '未知'}"})

    if debt_level == "高":
        signals.append({"name": "安全策略", "signal": "看空", "note": "资产负债率偏高，财务杠杆大"})
    elif debt_level == "低":
        signals.append({"name": "安全策略", "signal": "看多", "note": "资产负债率低，财务稳健"})
    else:
        signals.append({"name": "安全策略", "signal": "中性", "note": "负债率中性"})

    return signals


def _fmt_money(v: float) -> str:
    """把金额格式化为「亿 / 万」易读字符串。"""
    try:
        v = float(v)
    except Exception:
        return str(v)
    if abs(v) >= 1e8:
        return f"{v/1e8:.2f}亿"
    if abs(v) >= 1e4:
        return f"{v/1e4:.2f}万"
    return f"{v:.2f}"


if __name__ == "__main__":
    # 简单自测（不需要网络）：用假数据验证逻辑
    fake = {
        "technical": {"trend": "上升趋势", "indicators": {"MACD": "金叉", "KDJ": "金叉", "RSI": 55}},
        "fund_flow": {"flow_analysis": {"direction": "流入", "strength": "中", "main_inflow": 1.2e8},
                      "volume_analysis": {}, "main_fund_analysis": {}},
        "fundamental": {"financial": {"roe": 22, "debt_ratio": 40, "debt_level": "低"},
                        "valuation": {"pe": 18, "pb": 3, "ps": 5, "pe_level": "低", "pb_level": "中"},
                        "growth": {"revenue_growth": 0.2, "profit_growth": 0.25}},
        "conclusion": {"rating": "看多", "reason": "趋势向上", "risk": "市场系统性风险"},
    }
    import json
    print("策略信号:", json.dumps(build_strategy_analysis(fake), ensure_ascii=False, indent=2))
    print("买入建议:", json.dumps(build_buy_advice(fake), ensure_ascii=False, indent=2))
