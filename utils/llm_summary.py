#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把个股结构化数据 + 新闻，归纳成「决策仪表盘」的四段文字。

三通道降级，任何一个成功即返回：
  1) Cloudflare Workers AI（免密钥）—— POST 到 AI_SUMMARY_URL，由 Worker 内
     env.AI.run() 调模型；GitHub Actions 侧只需一个 URL，无需任何 API Key。
     可选 AI_SUMMARY_TOKEN 做 Bearer 鉴权。
  2) DeepSeek（预留兜底）—— DEEPSEEK_API_KEY 存 GitHub Secrets，
     调 https://api.deepseek.com/chat/completions（OpenAI 兼容）。
  3) 本地模板兜底 —— 都用不了时，从技术/资金/财务数据拼出稳定文字，
     保证卡片四块永远有内容，绝不因为 LLM 挂掉而开天窗。

输出统一为：
  {
    "sentiment":   "舆情情绪：...",
    "expectation": "业绩预期：...",
    "risks":       ["风险点...", ...],
    "catalysts":   ["利好...", ...],
    "latest":      "【最新消息】..."   # 无新闻时可为空串
  }
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import requests

BJ_TZ = timezone(timedelta(hours=8))
TIMEOUT = 20
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

SYSTEM_PROMPT = (
    "你是一名严谨的 A 股个股分析师。基于给定的结构化数据与新闻，输出四部分内容。\n"
    "只输出一个 JSON 对象，不要任何解释、不要 markdown 代码块。字段如下：\n"
    '{"sentiment":"一句话概括市场情绪与资金态度，60字内",'
    '"expectation":"一句话概括业绩与估值预期，60字内",'
    '"risks":["风险点1","风险点2","风险点3"],'
    '"catalysts":["利好1","利好2"],'
    '"latest":"一条最新消息，40字内；无新闻则空字符串"}\n'
    "要求：\n"
    "- 只能使用输入里出现的事实与数字，禁止编造消息、日期、公告或百分比。\n"
    "- risks 取 2~3 条，catalysts 取 1~3 条，每条一句话，点明具体数字/原因。\n"
    "- 风险侧重：资金流出、估值偏高、技术超买、业绩走弱等；利好侧重：业绩高增、"
    "资金流入、趋势向上、估值低位等。\n"
    "- 红涨绿跌；金额统一用「亿/万」，比率保留两位小数。\n"
    "- 不构成投资建议，不要出现「推荐买入/卖出」字样。"
)


# ---------------------------------------------------------------- 工具

def _build_user_payload(facts: Dict[str, Any], news: List[dict]) -> str:
    """把结构化数据摊成给模型看的紧凑文本；不塞入任何凭证/隐私。"""
    lines = [
        f"股票：{facts.get('name', '')}（{facts.get('code', '')}）"
        f" {facts.get('industry') or ''}",
        f"现价 {facts.get('price')} 涨跌幅 {facts.get('pct')} 总市值 {facts.get('cap')}",
        f"技术面：趋势={facts.get('trend')} MACD={facts.get('macd')} "
        f"KDJ={facts.get('kdj')} RSI={facts.get('rsi')}",
        f"资金面：主力净流入={facts.get('main_inflow')} "
        f"近20日净额={facts.get('flow_20d')} 流入天数={facts.get('flow_up_days')}",
        f"估值：PE={facts.get('pe')}（{facts.get('pe_level')}） "
        f"PB={facts.get('pb')}（{facts.get('pb_level')}）",
        f"财务：ROE={facts.get('roe')} 负债率={facts.get('debt_ratio')} "
        f"净利同比={facts.get('profit_growth')}",
    ]
    if facts.get("chips"):
        lines.append(f"筹码：平均成本={facts.get('chip_avg')} "
                     f"获利比例={facts.get('chip_profit')} "
                     f"集中度={facts.get('chip_concentration')}")
    if facts.get("advice"):
        lines.append(f"模型结论：{facts.get('advice')} "
                     f"看多{facts.get('bull_count')}项/看空{facts.get('bear_count')}项")
    if news:
        lines.append("近期新闻：")
        for i, item in enumerate(news[:8], 1):
            t = item.get("time") or ""
            lines.append(f"  {i}. [{t}] {item.get('title', '')} {item.get('summary', '')}")
    else:
        lines.append("近期新闻：无（不要编造，latest 字段留空）")
    return "\n".join(lines)


def _extract_json(text: str) -> Dict[str, Any]:
    """从模型输出里抠出 JSON：容忍 ```json 围栏与前后废话。"""
    if not text:
        raise ValueError("模型返回空文本")
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型输出里找不到 JSON 对象")
    obj = json.loads(cleaned[start:end + 1])
    if not isinstance(obj, dict):
        raise ValueError("模型输出不是 JSON 对象")
    return obj


def _sanitize(obj: Dict[str, Any], has_news: bool) -> Dict[str, Any]:
    """校验并规整字段，缺项补默认值，杜绝模型漏字段导致渲染崩。"""
    def _str(v, default=""):
        return str(v).strip() if v is not None else default

    def _list(v, cap):
        if isinstance(v, list):
            items = [str(x).strip() for x in v if str(x).strip()]
        elif isinstance(v, str) and v.strip():
            items = [v.strip()]
        else:
            items = []
        return items[:cap]

    sentiment = _str(obj.get("sentiment"))
    expectation = _str(obj.get("expectation"))
    risks = _list(obj.get("risks"), 3)
    catalysts = _list(obj.get("catalysts"), 3)
    latest = _str(obj.get("latest")) if has_news else ""

    if not sentiment and not expectation and not risks and not catalysts:
        raise ValueError("模型输出四段内容全空")
    return {
        "sentiment": sentiment, "expectation": expectation,
        "risks": risks, "catalysts": catalysts, "latest": latest,
    }


# ---------------------------------------------------------------- 通道 1：Cloudflare Workers AI

def _via_cloudflare(user_payload: str, has_news: bool) -> Dict[str, Any]:
    url = (os.environ.get("AI_SUMMARY_URL") or "").strip()
    if not url:
        raise RuntimeError("AI_SUMMARY_URL 未配置")
    headers = {"Content-Type": "application/json"}
    token = (os.environ.get("AI_SUMMARY_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.post(
        url, json={"system": SYSTEM_PROMPT, "user": user_payload},
        headers=headers, timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    text = data.get("text") or data.get("response") or ""
    if data.get("error"):
        raise RuntimeError(f"Worker 返回错误：{data['error']}")
    return _sanitize(_extract_json(text if isinstance(text, str) else json.dumps(text)), has_news)


# ---------------------------------------------------------------- 通道 2：DeepSeek 兜底

def _via_deepseek(user_payload: str, has_news: bool) -> Dict[str, Any]:
    api_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY 未配置")
    resp = requests.post(
        DEEPSEEK_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_payload},
            ],
            "temperature": 0.2,
            "max_tokens": 700,
            "response_format": {"type": "json_object"},
        },
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return _sanitize(_extract_json(content), has_news)


# ---------------------------------------------------------------- 通道 3：本地模板兜底

def _via_template(facts: Dict[str, Any], news: List[dict]) -> Dict[str, Any]:
    """没有 LLM 时用数据拼出稳定四段，保证卡片不缺块。"""
    def fv(key, default="—"):
        v = facts.get(key)
        return default if v is None or v == "" else str(v)

    pct = facts.get("pct")
    up = isinstance(pct, (int, float)) and pct >= 0
    main = facts.get("main_inflow")
    flow_dir = "净流入" if isinstance(main, (int, float)) and main >= 0 else "净流出"

    sentiment = (
        f"股价{fv('trend')}，当日{fv('pct')}，主力资金{flow_dir} {fv('main_inflow')}亿，"
        f"RSI {fv('rsi')}，短线情绪{'偏活跃' if up else '偏谨慎'}。"
    )
    expectation = (
        f"净利同比 {fv('profit_growth')}，PE {fv('pe')}（{fv('pe_level')}）、"
        f"PB {fv('pb')}、ROE {fv('roe')}%，业绩与估值{'具支撑' if up else '需观察'}。"
    )
    risks: List[str] = []
    if isinstance(main, (int, float)) and main < 0:
        risks.append(f"主力资金净流出 {abs(main):.2f}亿，需警惕短期抛压")
    pe_level = str(facts.get("pe_level") or "")
    if "高" in pe_level:
        risks.append(f"PE 处于高位（{fv('pe')}），注意估值回撤风险")
    rsi = facts.get("rsi")
    if isinstance(rsi, (int, float)) and rsi >= 70:
        risks.append(f"RSI={rsi:.1f} 进入超买区间，短期或有回调")
    debt = facts.get("debt_ratio")
    if isinstance(debt, (int, float)) and debt >= 60:
        risks.append(f"负债率 {debt:.1f}% 偏高，财务杠杆较大")
    if not risks:
        risks.append("多空因素相对均衡，仍需关注量能变化")

    catalysts: List[str] = []
    growth = facts.get("profit_growth")
    if isinstance(growth, (int, float)) and growth > 0.15:
        catalysts.append(f"净利同比 {growth * 100:+.2f}%，业绩高成长")
    if isinstance(main, (int, float)) and main > 0:
        catalysts.append(f"主力净流入 {main:.2f}亿，资金承接积极")
    if str(facts.get("trend") or "") == "上升趋势":
        catalysts.append("均线多头排列，中期趋势向上")
    if not catalysts:
        catalysts.append("暂无明确催化，等待量价配合")

    latest = ""
    if news:
        first = news[0]
        title = str(first.get("title") or "").strip()
        summary = str(first.get("summary") or "").strip()
        latest = f"【{first.get('source') or '最新'}】{title}"
        if summary and summary not in title:
            latest += f"：{summary[:40]}"
    return {
        "sentiment": sentiment, "expectation": expectation,
        "risks": risks[:3], "catalysts": catalysts[:3], "latest": latest,
    }


# ---------------------------------------------------------------- 最新动态日期

def _news_publish_time(news: List[dict]) -> str:
    """取最新一条新闻的发布时间，归一化成「YYYY-MM-DD HH:MM」（带年份）。

    - 只识别 news 里的真实 time 字段，绝不交给 LLM 生成，杜绝编造日期。
    - 东财给到时分则用时分（如 "2026-11-17 09:42"）；只有日期则只显示日期。
    - 只有「MM-DD」缺年份时按当前年补齐；无法识别则返回空串（卡片不显示日期）。
    """
    if not news:
        return ""
    raw = str(news[0].get("time") or "").strip()
    if not raw:
        return ""
    m = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", raw)
    if m:
        y, mo, d, hh, mm = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4), m.group(5)
        out = f"{y}-{mo:02d}-{d:02d}"
        if hh is not None and mm is not None:
            out += f" {int(hh):02d}:{mm}"
        return out
    m = re.search(r"(\d{1,2})[-/](\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", raw)
    if m:
        y = datetime.now(BJ_TZ).year
        mo, d, hh, mm = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4)
        out = f"{y}-{mo:02d}-{d:02d}"
        if hh is not None and mm is not None:
            out += f" {int(hh):02d}:{mm}"
        return out
    return ""


# ---------------------------------------------------------------- 统一入口

def summarize_insights(facts: Dict[str, Any], news: List[dict] | None = None) -> Dict[str, Any]:
    """按 Cloudflare → DeepSeek → 模板 顺序降级，正向结果立即返回。"""
    news = news or []
    has_news = bool(news)
    user_payload = _build_user_payload(facts, news)

    channels = (
        ("Cloudflare Workers AI", lambda: _via_cloudflare(user_payload, has_news)),
        ("DeepSeek", lambda: _via_deepseek(user_payload, has_news)),
    )
    result: Dict[str, Any] | None = None
    for label, fn in channels:
        try:
            result = fn()
            print(f"[INSIGHT] {label} 生成成功")
            break
        except Exception as exc:  # noqa: BLE001  单通道失败换下一个
            print(f"::warning::{label} 不可用（{type(exc).__name__}: {exc}），尝试下一个通道")

    if result is None:
        print("[INSIGHT] LLM 通道均不可用，改用本地模板兜底")
        result = _via_template(facts, news)

    # 日期来自真实新闻发布时间，与 latest 内容同源；没有 latest 就不显示日期。
    result["latest_time"] = _news_publish_time(news) if result.get("latest") else ""
    return result


if __name__ == "__main__":
    demo = {
        "name": "建设工业", "code": "002265", "industry": "汽车制造业",
        "price": 22.05, "pct": 0.0105, "cap": 227.79,
        "trend": "上升趋势", "macd": "多头", "kdj": "中性", "rsi": 71.5,
        "main_inflow": -0.32, "flow_20d": 1.89, "flow_up_days": 8,
        "pe": 69.2, "pe_level": "高", "pb": 5.8, "pb_level": "高",
        "roe": 3.2, "debt_ratio": 54.1, "profit_growth": 3.1562,
        "advice": "买入 60/100", "bull_count": 3, "bear_count": 3,
    }
    print(json.dumps(summarize_insights(demo), ensure_ascii=False, indent=2))
