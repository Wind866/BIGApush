#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
真实基本面与业绩数据获取。

替代 stock_analyzer 里写死的「模拟估值」(pe=price/10、pb=price/5、ps=price/8) 与
写死的成长率(0.1/0.15)。通过 akshare 拉取：
  - 实时估值：市盈率(动态)/市净率/市销率/总市值/流通市值  (stock_individual_info_em)
  - 财务与成长：营业总收入/净利润/净资产收益率/资产负债率/营收同比/净利润同比
                 (stock_financial_analysis_indicator)

所有取值失败均返回空 dict，由调用方回退到既有的模拟值，保证页面不崩。
"""
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.akshare_retry import akshare_call_with_retry


def _safe(func, *args, **kwargs):
    """带重试地调用 akshare 函数，失败返回 None。"""
    try:
        return akshare_call_with_retry(func, *args, **kwargs)
    except Exception as e:  # noqa: BLE001
        print(f"[real_fundamentals] akshare 调用失败 {getattr(func, '__name__', func)}: {e}")
        return None


def _to_float(value: Any) -> Optional[float]:
    """把 akshare 返回的「1,234.56」/ None / '' 转成 float，失败返回 None。"""
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").replace("%", "").replace("亿", "")
                     .replace("万", "").strip())
    except Exception:
        return None


def _pick(df, *keywords) -> Optional[float]:
    """在 DataFrame 的列名里按关键词模糊匹配，返回首列最新一期的数值。"""
    if df is None or df.empty:
        return None
    cols = list(df.columns)
    for kw in keywords:
        for c in cols:
            if kw in str(c):
                try:
                    return _to_float(df.iloc[0][c])
                except Exception:
                    continue
    return None


def get_real_valuation(stock_code: str) -> Dict[str, Any]:
    """实时估值：PE(动态)/PB/PS/总市值/流通市值。

    Returns:
        dict，可能包含 pe/pb/ps/market_cap/float_market_cap；取不到的键不存在。
    """
    try:
        import akshare as ak
        df = _safe(ak.stock_individual_info_em, symbol=stock_code)
        if df is None or df.empty:
            return {}
        # df 形如 item/value 两列
        if "item" in df.columns and "value" in df.columns:
            m = dict(zip(df["item"], df["value"]))
            out = {}
            for key, item_name in (("pe", "市盈率"), ("pb", "市净率"), ("ps", "市销率"),
                                   ("market_cap", "总市值"), ("float_market_cap", "流通市值")):
                if item_name in m:
                    val = _to_float(m[item_name])
                    if val is not None:
                        out[key] = val
            return out
        return {}
    except Exception as e:  # noqa: BLE001
        print(f"[real_fundamentals] 估值获取失败 {stock_code}: {e}")
        return {}


def get_real_financials(stock_code: str) -> Dict[str, Any]:
    """财务与成长：营收/净利润/ROE/资产负债率 + 同比营收/利润增速。

    Returns:
        dict，可能包含 revenue/profit/roe/debt_ratio/revenue_growth/profit_growth。
    """
    try:
        import akshare as ak
        df = _safe(ak.stock_financial_analysis_indicator, symbol=stock_code)
        if df is None or df.empty:
            return {}
        out = {}
        out["revenue"] = _pick(df, "营业总收入")
        out["profit"] = _pick(df, "净利润")
        out["roe"] = _pick(df, "净资产收益率")
        out["debt_ratio"] = _pick(df, "资产负债率")
        # 同比/增速类字段：akshare 返回的是「百分数」(如 25.3 表示 25.3%)，
        # 统一换算成小数(0.253)以匹配 advisor 的阈值语义(>0.15 视为高成长)。
        rev_g = _pick(df, "营业总收入同比增长率", "营业收入同比增长率")
        pro_g = _pick(df, "净利润同比增长率")
        if rev_g is not None:
            out["revenue_growth"] = rev_g / 100.0
        if pro_g is not None:
            out["profit_growth"] = pro_g / 100.0
        # 只保留非 None 的键
        return {k: v for k, v in out.items() if v is not None}
    except Exception as e:  # noqa: BLE001
        print(f"[real_fundamentals] 财务获取失败 {stock_code}: {e}")
        return {}


def enrich_fundamental(stock_code: str, fundamental: Dict[str, Any]) -> Dict[str, Any]:
    """把真实估值/财务合并进既有的 fundamental 结构（只覆盖取到的字段）。

    Args:
        stock_code: 股票代码
        fundamental: stock_analyzer.analyze() 返回的 fundamental 子结构

    Returns:
        合并后的 fundamental 结构（含 valuation/financial/growth）。
    """
    fund = dict(fundamental or {})
    real_val = get_real_valuation(stock_code)
    real_fin = get_real_financials(stock_code)

    if real_val:
        val = dict(fund.get("valuation") or {})
        # 真实 PE/PB/PS 覆盖模拟值；同时补 market_cap
        for k in ("pe", "pb", "ps", "market_cap", "float_market_cap"):
            if real_val.get(k) is not None:
                val[k] = real_val[k]
        # 重新评估估值水位（基于真实 PE）
        pe = val.get("pe")
        if pe is not None:
            val["pe_level"] = "低" if pe < 20 else "中" if pe < 40 else "高"
        pb = val.get("pb")
        if pb is not None:
            val["pb_level"] = "低" if pb < 2 else "中" if pb < 4 else "高"
        fund["valuation"] = val

    if real_fin:
        fin = dict(fund.get("financial") or {})
        for k in ("revenue", "profit", "roe", "debt_ratio"):
            if real_fin.get(k) is not None:
                fin[k] = real_fin[k]
        # 由真实资产负债率推导负债水位，供「安全策略」信号使用
        dr = fin.get("debt_ratio")
        if dr is not None:
            fin["debt_level"] = "高" if dr > 60 else "低" if dr < 40 else "中"
        fund["financial"] = fin

        gr = dict(fund.get("growth") or {})
        if real_fin.get("revenue_growth") is not None:
            gr["revenue_growth"] = real_fin["revenue_growth"]
        if real_fin.get("profit_growth") is not None:
            gr["profit_growth"] = real_fin["profit_growth"]
        if real_fin.get("profit_growth") is not None:
            gr["growth_level"] = ("高" if real_fin["profit_growth"] > 0.2
                                  else "中" if real_fin["profit_growth"] > 0 else "低")
        fund["growth"] = gr

    return fund


if __name__ == "__main__":
    import json
    for code in ("600519", "000001"):
        print(code, "valuation:", json.dumps(get_real_valuation(code), ensure_ascii=False))
        print(code, "financials:", json.dumps(get_real_financials(code), ensure_ascii=False))
