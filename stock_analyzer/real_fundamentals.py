#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
真实基本面与业绩数据获取。

替代 stock_analyzer 里写死的「模拟估值」(pe=price/10、pb=price/5、ps=price/8) 与
写死的成长率(0.1/0.15)。通过 akshare 拉取：
  - 实时估值：市盈率(TTM)/市净率/市销率/总市值
                主源 stock_zh_valuation_baidu(百度，PE(TTM)+PB+总市值，数据中心友好)；
                东财 push2 会拦截云主机 IP(GitHub Actions)，仅作兜底；
                PS 由「总市值 ÷ TTM 营业收入(新浪利润表)」计算
  - 财务与成长：营业收入/净利润(stock_financial_report_sina 利润表) +
                 净资产收益率/资产负债率/营收·净利润同比(stock_financial_analysis_indicator)

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
    """把 akshare 返回的「1,234.56」/ None / '' / NaN 转成 float，失败返回 None。"""
    if value is None or value == "":
        return None
    try:
        v = float(str(value).replace(",", "").replace("%", "").replace("亿", "")
                   .replace("万", "").strip())
        if v != v:  # NaN 检测
            return None
        return v
    except Exception:
        return None


def _to_money_yuan(value: Any) -> Optional[float]:
    """把 akshare 市值类数值统一转成「元」（前端 fmtMoney 按元展示）。

    东财 stock_individual_info_em 返回如 "17941.92亿" / "1234.56万" 的字符串；
    百度 stock_zh_valuation_baidu 返回纯数值（单位：亿）。本函数统一换算成元。
    """
    if value is None or value == "":
        return None
    s = str(value).strip()
    try:
        if "亿" in s:
            return float(s.replace(",", "").replace("亿", "").replace("万", "")) * 1e8
        if "万" in s:
            return float(s.replace(",", "").replace("万", "").replace("亿", "")) * 1e4
        # 百度等纯数值（单位：亿）或已是元的兜底
        num = float(s.replace(",", ""))
        if num < 1e6:  # 量级过小，判定为「亿」数值
            return num * 1e8
        return num
    except Exception:
        return None


def _pick(df, *keywords, latest: bool = True) -> Optional[float]:
    """在 DataFrame 的列名里按关键词取值。

    - 优先「精确匹配」：去掉单位/括号后等于关键词的列（避免 净利润 命中 总资产净利润率）。
    - 否则退化为「包含匹配」，并排除 总资产净利润率 这类派生指标抢匹配。
    - latest=True 取最新一期：东财分析指标最新在 iloc[-1]；latest=False 取首行：
      新浪利润表最新一期在 iloc[0]。
    """
    if df is None or df.empty:
        return None
    cols = list(df.columns)
    row = df.iloc[-1] if latest else df.iloc[0]

    def base(c: str) -> str:
        return (str(c).replace('(元)', '').replace('(%)', '')
                     .replace('（元）', '').replace('（%）', '').strip())

    # 1) 精确匹配（去单位后等于关键词）
    for kw in keywords:
        for c in cols:
            if base(c) == kw:
                v = _to_float(row[c])
                if v is not None:
                    return v
    # 2) 退化的包含匹配（排除明显派生指标列）
    for kw in keywords:
        for c in cols:
            s = str(c)
            if kw in s and "总资产净利润率" not in s:
                v = _to_float(row[c])
                if v is not None:
                    return v
    return None


def _baidu_valuation(stock_code: str) -> Dict[str, Any]:
    """百度估值：总市值 + 市净率 + 市盈率(TTM)。

    百度接口对数据中心 IP 友好（东财 push2 会拦截云主机 IP，本项目部署在
    GitHub Actions 上，故以百度为主源）。指标名必须写成「市盈率(TTM)」，
    写成「市盈率」/「市盈率ttm」会因为返回结构为空而解析失败。

    Returns:
        dict，可能包含 market_cap(元) / pb / pe；取不到的键不存在。
    """
    out: Dict[str, Any] = {}
    try:
        import akshare as ak
        # 总市值（百度返回单位：亿元 -> 统一转元）
        mc = _safe(ak.stock_zh_valuation_baidu, symbol=stock_code,
                   indicator="总市值", period="近一年")
        if mc is not None and not mc.empty and "value" in mc.columns:
            v = _to_money_yuan(mc.iloc[-1]["value"])
            if v is not None:
                out["market_cap"] = v
        # 市净率
        pb = _safe(ak.stock_zh_valuation_baidu, symbol=stock_code,
                   indicator="市净率", period="近一年")
        if pb is not None and not pb.empty and "value" in pb.columns:
            v = _to_float(pb.iloc[-1]["value"])
            if v is not None:
                out["pb"] = v
        # 市盈率(TTM)：指标名必须带 (TTM) 后缀，否则百度返回空结构解析失败
        pe = _safe(ak.stock_zh_valuation_baidu, symbol=stock_code,
                   indicator="市盈率(TTM)", period="近一年")
        if pe is not None and not pe.empty and "value" in pe.columns:
            v = _to_float(pe.iloc[-1]["value"])
            if v is not None:
                out["pe"] = v
    except Exception as e:  # noqa: BLE001
        print(f"[real_fundamentals] 百度估值获取失败 {stock_code}: {e}")
    return out


def _ttm_revenue(stock_code: str) -> Optional[float]:
    """用新浪利润表计算 TTM 营业收入（元）。

    算法：TTM = 最近年报 + 本期累计 - 去年同期累计；若最新一期即年报则直接取年报。
    新浪利润表「报告日」为 YYYYMMDD、「营业总收入」为元，最新一期在 iloc[0]。
    失败返回 None。
    """
    try:
        import akshare as ak
        inc = _safe(ak.stock_financial_report_sina, stock=stock_code, symbol="利润表")
        if inc is None or inc.empty or "报告日" not in inc.columns:
            return None
        rev_col = ("营业总收入" if "营业总收入" in inc.columns
                   else ("营业收入" if "营业收入" in inc.columns else None))
        if rev_col is None:
            return None
        rows = []
        for _, r in inc.iterrows():
            try:
                d = int(str(r["报告日"]).strip())
            except Exception:
                continue
            v = _to_float(r[rev_col])
            if v is not None:
                rows.append((d, v))
        if not rows:
            return None
        rows.sort(reverse=True)
        latest_d, latest_v = rows[0]
        if latest_d % 10000 == 1231:            # 最新一期就是年报
            return latest_v
        fy = next(((d, v) for d, v in rows if d < latest_d and d % 10000 == 1231), None)
        prior_d = latest_d - 10000             # 去年同期
        prior_v = next((v for d, v in rows if d == prior_d), None)
        if fy and prior_v is not None:
            return fy[1] + latest_v - prior_v
        return None
    except Exception as e:  # noqa: BLE001
        print(f"[real_fundamentals] TTM 营收计算失败 {stock_code}: {e}")
        return None


def get_real_valuation(stock_code: str) -> Dict[str, Any]:
    """实时估值：PE(TTM)/PB/PS/总市值。

    多源策略（适配 GitHub Actions 等数据中心 IP，东财 push2 会拦截云主机请求）：
      1) 主源 百度 stock_zh_valuation_baidu：PE(TTM) + PB + 总市值，数据中心友好；
      2) 主源完全取不到时兜底 东财 stock_individual_info_em（本地/住宅 IP 可达）；
      3) PS 由「总市值 ÷ TTM 营业收入(新浪)」计算，全环境可用；
         PS 计算失败时不设置该键，由调用方回退原估算值。
    所有取值失败均返回空 dict，由调用方回退到既有模拟值，保证页面不崩。
    """
    # 1) 主源：百度（PE(TTM)/PB/总市值）
    out = _baidu_valuation(stock_code)

    # 2) 兜底：东财（仅当百度完全取不到时；数据中心 IP 通常失败，故不做首选）
    if not out:
        try:
            import akshare as ak
            df = _safe(ak.stock_individual_info_em, symbol=stock_code)
            if df is not None and not df.empty and "item" in df.columns and "value" in df.columns:
                m = dict(zip(df["item"], df["value"]))
                for key, item_name in (("pe", "市盈率"), ("pb", "市净率"), ("ps", "市销率"),
                                       ("market_cap", "总市值"), ("float_market_cap", "流通市值")):
                    if item_name in m:
                        val = (_to_money_yuan(m[item_name]) if key in ("market_cap", "float_market_cap")
                               else _to_float(m[item_name]))
                        if val is not None:
                            out[key] = val
        except Exception as e:  # noqa: BLE001
            print(f"[real_fundamentals] 东财估值兜底失败 {stock_code}: {e}")

    # 3) PS：市值 / TTM 营收
    if out.get("ps") is None and out.get("market_cap"):
        ttm = _ttm_revenue(stock_code)
        if ttm and ttm > 0:
            out["ps"] = out["market_cap"] / ttm

    return out


def get_real_financials(stock_code: str) -> Dict[str, Any]:
    """财务与成长：营收/净利润/ROE/资产负债率 + 同比营收/利润增速。

    数据来源：
      - 营收/净利润：新浪利润表(stock_financial_report_sina)，最新一期在 iloc[0]。
      - ROE/资产负债率/增速：东财财务分析指标(stock_financial_analysis_indicator)，
        最新一期在 iloc[-1]。

    Returns:
        dict，可能包含 revenue/profit/roe/debt_ratio/revenue_growth/profit_growth。
    """
    out: Dict[str, Any] = {}

    # 1) 利润表（新浪）：营业收入 / 净利润
    try:
        import akshare as ak
        inc = _safe(ak.stock_financial_report_sina, stock=stock_code, symbol="利润表")
        if inc is not None and not inc.empty:
            rev = _pick(inc, "营业总收入", "营业收入", latest=False)
            prof = _pick(inc, "净利润", "归属于母公司所有者的净利润", latest=False)
            if rev is not None:
                out["revenue"] = rev
            if prof is not None:
                out["profit"] = prof
    except Exception as e:  # noqa: BLE001
        print(f"[real_fundamentals] 利润表获取失败 {stock_code}: {e}")

    # 2) 财务分析指标（东财）：ROE / 资产负债率 / 增速
    try:
        import akshare as ak
        df = _safe(ak.stock_financial_analysis_indicator, symbol=stock_code)
        if df is not None and not df.empty:
            roe = _pick(df, "净资产收益率")
            dr = _pick(df, "资产负债率")
            pgr = _pick(df, "净利润增长率")
            rgr = _pick(df, "主营业务收入增长率", "营业总收入同比增长率", "营业收入同比增长率")
            if roe is not None:
                out["roe"] = roe
            if dr is not None:
                out["debt_ratio"] = dr
            # 增速字段为百分数(如 15.3 表示 15.3%)，换算成小数以匹配 advisor 阈值
            if pgr is not None:
                out["profit_growth"] = pgr / 100.0
            if rgr is not None:
                out["revenue_growth"] = rgr / 100.0
    except Exception as e:  # noqa: BLE001
        print(f"[real_fundamentals] 财务分析指标获取失败 {stock_code}: {e}")

    return {k: v for k, v in out.items() if v is not None}


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
