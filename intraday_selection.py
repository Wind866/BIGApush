#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
盘中选股观察池（北京时间 13:00）

设计目标：
1. 独立于主程序 main.py —— 不依赖 akshare / 数据库 / 首次全量初始化，
   在 GitHub Actions 上 10 秒内跑完，不会因主流程超时而失败。
2. 只用 Python 标准库，workflow 无需 pip install。
3. 基于当日实时快照选股，明确标注「盘中快照」，不写入主程序数据库，
   因此不会污染收盘选股的「与上一交易日对比」与历史记录。
4. 自带精确交易日判断（用行情时间戳比对北京日期，可识别节假日），
   非交易日静默跳过推送。

口径说明：
- 收盘选股是「已收盘 K 线 + 15 种形态策略」的定稿信号；
- 盘中选股是「实时量价 + 主力资金 + 板块共振」的观察池，属盘中强势股跟踪，
  午后仍可能变盘，不等同于收盘选股结果，也不构成投资建议。

用法：
    python intraday_selection.py                 # 盘中选股并推送飞书
    python intraday_selection.py --dry-run       # 本地试跑，只打印卡片不推送
    python intraday_selection.py --check-trading-day  # 仅输出交易日判断（供 workflow 预检）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

BJ_TZ = timezone(timedelta(hours=8))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import biga_sources as bs  # noqa: E402  多源容灾层（东财/同花顺/新浪/腾讯）

MIN_PCT = 3.0        # 只扫描涨幅 >= 3% 的强势股
SCAN_FLOOR = 2.0     # 分页扫描到涨幅低于该值即停止
MAX_PAGES = 20       # 上限 2000 只，防止极端行情下请求过多
TOP_N = 10           # 卡片展示数量

# 评分权重（合计 1.0，可按需调整）
WEIGHTS = {
    "mom": 0.20,     # 涨幅动量
    "fund": 0.28,    # 主力净流入
    "super": 0.14,   # 超大单
    "vol": 0.12,     # 量比
    "turn": 0.08,    # 换手
    "sector": 0.10,  # 板块共振
    "cap": 0.08,     # 流通市值
}

# 资金接口限流、降级到新浪行情时使用的「纯量价」权重（合计 1.0）
WEIGHTS_NO_FUND = {
    "mom": 0.32, "fund": 0.0, "super": 0.0, "vol": 0.25,
    "turn": 0.15, "sector": 0.0, "cap": 0.28,
}



def now_bj() -> datetime:
    return datetime.now(BJ_TZ)


def _num(v: Any, default: float | None = None) -> float | None:
    """东财停牌/异常字段常返回 '-' 或 None，统一安全转换。"""
    if v is None or v == "-" or v == "":
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:  # NaN
        return default
    return f


def fetch_index_timestamp() -> int:
    """多源交易日行情时点：东财多域名 → 腾讯指数时间戳。"""
    ts, label = bs.fetch_index_timestamp()
    print(f"[交易日] 行情时点来源：{label}")
    return ts


def check_trading_day() -> tuple[bool, str]:
    """
    返回 (是否交易日, 说明)。
    先用周末规则快速排除，再用行情时间戳的北京日期比对识别法定节假日。
    指数日历全部失效时保守放行（只按周末规则），避免网络波动导致漏跑。
    """
    bj = now_bj()
    if bj.weekday() >= 5:
        return False, f"{bj:%Y-%m-%d} 为周末"
    try:
        ts = fetch_index_timestamp()
        quote_day = datetime.fromtimestamp(ts, BJ_TZ).date()
        if quote_day != bj.date():
            return False, f"行情最新日期 {quote_day}，今日 {bj.date()} 非交易日（节假日或休市）"
        return True, f"行情时间戳 {datetime.fromtimestamp(ts, BJ_TZ):%Y-%m-%d %H:%M:%S} 为当日"
    except Exception as exc:  # noqa: BLE001
        return True, f"交易日历接口全部不可用（{type(exc).__name__}），按周末规则放行"


def fetch_market_snapshot():
    """多源强势股快照，返回 (rows, 行情时点, 评分口径, Snapshot)。

    降级顺序：东财 clist 多域名 → 新浪涨幅榜 → 同花顺涨停池，
    备用池再依次用东财 ulist.np / 腾讯行情 / 新浪资金流补齐字段。
    只有东财口径参与资金评分，避免跨口径分数被误对比。
    """
    snap = bs.fetch_snapshot(scan_floor=SCAN_FLOOR, max_pages=MAX_PAGES)
    return snap.rows, snap.quote_ts, snap.source, snap


def is_limit_up(code: str, pct: float, name: str) -> bool:
    """按板块区分涨停阈值（ST 5%、主板 10%、双创 20%）。"""
    if "ST" in name.upper() or "退" in name:
        return pct >= 4.8
    if code.startswith(("300", "301", "688", "689")):
        return pct >= 19.5
    return pct >= 9.7


def _clip(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _score_mom(pct: float, sealed: bool) -> float:
    if sealed:
        return 78.0  # 已封板：强度高但难以买入
    if pct < 3:
        return 20.0
    if pct <= 7:
        return 60 + (pct - 3) * 10
    if pct <= 9.5:
        return 100.0
    return 88.0


def _score_fund(main_pct: float, main_net: float, float_mv: float) -> float:
    base = 35 + _clip(main_pct) * 3.2
    if float_mv > 0:
        base += _clip(main_net / float_mv * 100 * 8, 0, 15)
    return _clip(base)


def _score_vol(vr: float) -> float:
    if vr < 1:
        return 35.0
    if vr < 2:
        return 55 + (vr - 1) * 20
    if vr <= 5:
        return 85 + (vr - 2) * 5
    if vr <= 10:
        return 70.0
    return 42.0


def _score_turn(to: float) -> float:
    if to < 2:
        return 40.0
    if to < 5:
        return 65 + (to - 2) * 8
    if to <= 15:
        return 100.0
    if to <= 25:
        return 65.0
    return 38.0


def _score_cap(float_mv_yi: float) -> float:
    if 30 <= float_mv_yi <= 200:
        return 100.0
    if 200 < float_mv_yi <= 500:
        return 76.0
    if 20 <= float_mv_yi < 30:
        return 72.0
    if float_mv_yi > 500:
        return 48.0
    return 52.0


SOURCE_LABELS = {
    "eastmoney": "东方财富（完整口径）",
    "mixed": "备用源混合口径",
    "sina": "新浪财经（量价）",
    "ths": "同花顺涨停池（量价）",
    "tencent": "腾讯证券（量价）",
}


def build_pool(rows: list[dict], source: str = "eastmoney",
               snapshot=None) -> tuple[list[dict], dict]:
    """清洗 + 打分 + 排序，返回 (观察池, 市场概览)。

    source=eastmoney：东财完整口径（主力资金 / 超大单 / 行业共振参与评分）。
    source=mixed/sina/ths/tencent：备用源口径，只用量价评分。备用源即使
    补齐了资金数据，也仅作展示参考，绝不混进评分（口径不等价）。
    """
    has_fund = source == "eastmoney"
    cleaned: list[dict] = []
    limit_up_count = 0
    seen: set[str] = set()

    for r in rows:
        code = str(r.get("f12") or "").strip()
        name = str(r.get("f14") or "").strip()
        if not code or not name or code in seen:
            continue
        # 排除北交所、ST、退市
        if code.startswith(("8", "4", "9")):
            continue
        upper = name.upper()
        if "ST" in upper or "退" in name or "PT" in upper:
            continue

        pct = _num(r.get("f3"))
        price = _num(r.get("f2"))
        turnover = _num(r.get("f8"))
        vol_ratio = _num(r.get("f10")) or 0.0
        amount = _num(r.get("f6"))
        float_mv = _num(r.get("f21"))
        main_net = _num(r.get("f62"))
        main_pct = _num(r.get("f184"))
        super_net = _num(r.get("f66"))
        super_pct = _num(r.get("f69"))
        industry = str(r.get("f100") or "其他").strip() or "其他"

        if None in (pct, price, turnover, amount, float_mv):
            continue
        if is_limit_up(code, pct, name):
            limit_up_count += 1
        if pct < MIN_PCT or pct > 21 or price <= 0:
            continue
        # 主力资金必须净流入（降级源无资金字段时跳过该条件）
        if has_fund and (main_net is None or main_pct is None
                         or main_net <= 0 or main_pct <= 0):
            continue
        # 剔除流动性过差与市值异常标的
        if amount < 5e7:
            continue
        float_mv_yi = float_mv / 1e8
        if float_mv_yi < 15 or float_mv_yi > 1500 or turnover < 1:
            continue

        seen.add(code)
        cleaned.append({
            "code": code, "name": name, "industry": industry,
            "price": price, "pct": pct, "turnover": turnover,
            "vol_ratio": vol_ratio, "amount_yi": amount / 1e8,
            "float_mv_yi": float_mv_yi,
            "main_net_yi": (main_net or 0) / 1e8, "main_pct": main_pct or 0,
            "super_net_yi": (super_net or 0) / 1e8,
            "super_pct": super_pct or 0,
            "has_fund": has_fund,
            "sealed": is_limit_up(code, pct, name),
        })

    # 板块共振：只有真实行业字段可用时才计算。备用源的 f100 一律为「其他」，
    # 若照常计数会把全市场误判成同一板块，产生虚假共振标签与虚高分数。
    sector_ok = has_fund and any(c["industry"] != "其他" for c in cleaned)
    sector_count: dict[str, int] = {}
    if sector_ok:
        for c in cleaned:
            if c["industry"] != "其他":
                sector_count[c["industry"]] = sector_count.get(c["industry"], 0) + 1

    weights = WEIGHTS if has_fund else WEIGHTS_NO_FUND
    pool: list[dict] = []
    for c in cleaned:
        sealed = c["sealed"]
        factors = {
            # 降级口径缺少资金/量比/行业，用连续动量曲线避免大量并列分数
            "mom": (_score_mom(c["pct"], sealed) if has_fund
                    else _clip(50 + c["pct"] * 5.5)),
            "fund": _score_fund(c["main_pct"], c["main_net_yi"] * 1e8,
                                c["float_mv_yi"] * 1e8) if has_fund else 60.0,
            "super": _clip(38 + (c["super_pct"] or 0) * 4.5) if has_fund else 60.0,
            # 腾讯行情补齐后备用源也有真实量比，优先用真实值；
            # 完全没有量比时才用「成交额 / 流通市值」近似量能活跃度。
            "vol": (_score_vol(c["vol_ratio"]) if c["vol_ratio"] > 0 else
                    _clip(40 + c["amount_yi"] / max(c["float_mv_yi"], 1) * 100 * 3)),
            "turn": _score_turn(c["turnover"]),
            "sector": {1: 40, 2: 65, 3: 85}.get(sector_count.get(c["industry"], 1), 100)
                      if sector_ok else 50.0,
            "cap": _score_cap(c["float_mv_yi"]),
        }
        score = sum(factors[k] * w for k, w in weights.items())

        tags: list[str] = []
        penalty = 0.0
        if sealed:
            tags.append("已封板·难买入")
            penalty += 12
        if c["turnover"] > 25:
            tags.append("高换手")
            penalty += 8
        if c["vol_ratio"] > 10:
            tags.append("异常放量")
            penalty += 5
        if sector_ok and sector_count.get(c["industry"], 1) >= 3:
            tags.append("板块共振")
        if has_fund and c["super_pct"] and c["main_pct"] \
                and c["super_pct"] >= c["main_pct"] * 0.8:
            tags.append("超大单主导")
        if not tags:
            tags.append("资金流入" if has_fund else "量价强势")
        score = _clip(score - penalty)

        c["score"] = round(score, 1)
        c["grade"] = "A" if score >= 75 else "B" if score >= 65 else "C" if score >= 55 else "D"
        c["tags"] = tags
        c["sector_peers"] = sector_count.get(c["industry"], 1)
        c["factors"] = {k: round(v, 1) for k, v in factors.items()}
        pool.append(c)

    pool.sort(key=lambda x: (-x["score"], -x["main_net_yi"]))

    top_sectors = sorted(
        [{"name": k, "count": v} for k, v in sector_count.items() if v >= 2],
        key=lambda x: -x["count"],
    )[:5]
    overview = {
        "scanned": len(rows),
        "qualified": len(cleaned),
        "limit_up": limit_up_count,
        "top_sectors": top_sectors,
        "source": source,
        "has_fund": has_fund,
        "source_label": snapshot.label if snapshot else SOURCE_LABELS.get(source, source),
        "quality": snapshot.quality if snapshot else ("full" if has_fund else "minimal"),
        "fund_comparable": snapshot.fund_comparable if snapshot else has_fund,
        "fund_reference": (not has_fund) and any(r.get("f62") is not None for r in rows),
        "notes": list(snapshot.notes) if snapshot else [],
        "avg_main_net_yi": round(
            sum(c["main_net_yi"] for c in pool[:TOP_N]) / max(1, len(pool[:TOP_N])), 2
        ),
    }
    return pool, overview


def build_card(pool: list[dict], overview: dict, quote_ts: int) -> dict:
    """飞书交互卡片。"""
    bj = now_bj()
    qt = datetime.fromtimestamp(quote_ts, BJ_TZ) if quote_ts else bj
    date_txt = bj.strftime("%Y-%m-%d")
    time_txt = bj.strftime("%H:%M")
    has_fund = overview.get("has_fund", True)

    label = overview.get("source_label") or SOURCE_LABELS.get(
        overview.get("source", ""), "备用源")
    fund_reference = overview.get("fund_reference", False)
    if has_fund:
        fund_summary = f"Top{TOP_N} 平均主力净额 **{overview['avg_main_net_yi']:+.2f} 亿**"
    elif fund_reference:
        fund_summary = (f"**数据源：{label}（量价评分口径）** · "
                        "备用渠道资金数据仅作参考、未计入评分")
    else:
        fund_summary = f"**数据源：{label}（量价口径，本期无主力资金数据）**"
    head = (
        f"**数据时点 {qt:%H:%M:%S}（北京时间）** · 扫描强势股 {overview['scanned']} 只 / "
        f"涨停约 {overview['limit_up']} 家 / 入池 {overview['qualified']} 只\n"
        f"{fund_summary}"
    )

    if pool:
        rows = []
        for i, c in enumerate(pool[:TOP_N], 1):
            color = "red" if c["pct"] > 0 else "green"
            if has_fund:
                detail = (
                    f"{c['industry']} · 量比{c['vol_ratio']:.1f} · 换手{c['turnover']:.1f}% · "
                    f"主力 **{c['main_net_yi']:+.2f}亿 / {c['main_pct']:+.1f}%**"
                )
            else:
                detail = (
                    f"换手{c['turnover']:.1f}% · 成交额{c['amount_yi']:.2f}亿 · "
                    f"流通市值{c['float_mv_yi']:.0f}亿"
                )
                if fund_reference and c["main_net_yi"]:
                    detail += f" · 参考主力 {c['main_net_yi']:+.2f}亿"
            rows.append(
                f"**{i}. {c['name']} `{c['code']}`　{c['score']}分 / {c['grade']}**\n"
                f"<font color='{color}'>{c['pct']:+.2f}%</font> · {detail}\n"
                f"<font color='grey'>{(' · '.join(c['tags']))}</font>"
            )
        pool_md = "\n\n".join(rows)
    else:
        pool_md = "今日盘中未筛出符合条件的标的，建议以观察为主。"

    if overview["top_sectors"]:
        sector_md = "　".join(
            f"**{s['name']}** {s['count']}只" for s in overview["top_sectors"]
        )
    else:
        sector_md = ("无明显的行业共振" if has_fund
                     else f"{label} 不提供可比的行业字段，本期跳过板块共振分析")

    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": head}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": "**🎯 盘中强势观察池（Top 10）**"}},
        {"tag": "div", "text": {"tag": "lark_md", "content": pool_md}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**🔥 板块共振**\n{sector_md}"}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content":
            "<font color='orange'>⚠️ 这是 13:00 盘中实时快照，午后仍可能变盘；"
            "以 15:30 收盘选股为定稿信号，本推送不等同于收盘选股结果。</font>"}},
    ]
    if not has_fund:
        extra = ("备用渠道已补到资金拆解，但与东财口径不等价，仅作展示、未计入评分；"
                 if fund_reference else "本期无主力资金拆解；")
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content":
            f"<font color='orange'>⚠️ 主数据源降级至 {label}"
            f"（{overview.get('quality', 'minimal')}）：本期按量价口径评分"
            f"（涨幅/量比/换手/成交额/流通市值），{extra}"
            "不含行业共振，分数不可与历史东财口径直接比较。</font>"}})
        if overview.get("notes"):
            elements.append({"tag": "note", "elements": [{"tag": "plain_text",
                "content": "容灾轨迹：" + "；".join(overview["notes"][:3])}]})
    src_note = (
        "数据来源：东方财富实时行情（多域名容灾）｜评分：涨幅/量比/换手/主力资金/超大单/板块共振/市值加权。"
        "主力资金为按成交单笔金额推断的统计量，非席位真实资金。"
        if has_fund else
        f"数据来源：{label}｜评分：涨幅/量比/换手/成交额/流通市值加权。"
        "降级口径分数仅用于当日内部排序，不与历史东财口径比较。"
    )
    elements += [
        {"tag": "note", "elements": [{"tag": "plain_text", "content":
            src_note + f"生成于 {date_txt} {time_txt}（北京时间）。"}]},
        {"tag": "note", "elements": [{"tag": "plain_text", "content":
            "仅供研究复盘，不构成投资建议。市场有风险，投资需谨慎。"}]},
    ]

    # 在 Actions 中运行时附上「查看运行日志」按钮
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if repo and run_id:
        elements.append({
            "tag": "action",
            "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": "查看本次运行日志"},
                "type": "primary",
                "url": f"{server}/{repo}/actions/runs/{run_id}",
            }],
        })

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True, "enable_forward": True},
            "header": {
                "template": "orange",
                "title": {"tag": "plain_text",
                          "content": f"📊 盘中选股观察池 · {date_txt} {time_txt}"},
                "subtitle": {"tag": "plain_text",
                             "content": f"入池 {overview['qualified']} 只 | 涨停 {overview['limit_up']} 家 | 盘中快照"},
            },
            "elements": elements,
        },
    }


def card_to_text(pool: list[dict], overview: dict, quote_ts: int) -> str:
    """卡片推送失败时降级为纯文本，保证消息不丢。"""
    bj = now_bj()
    qt = datetime.fromtimestamp(quote_ts, BJ_TZ) if quote_ts else bj
    has_fund = overview.get("has_fund", True)
    lines = [
        f"📊 盘中选股观察池 · {bj:%Y-%m-%d %H:%M}（盘中快照）",
        f"数据时点 {qt:%H:%M:%S} | 扫描 {overview['scanned']} 只 | 涨停约 {overview['limit_up']} 家 | 入池 {overview['qualified']} 只",
    ]
    label = overview.get("source_label") or "备用源"
    if not has_fund:
        extra = ("备用资金数据仅作参考、未计入评分" if overview.get("fund_reference")
                 else "本期无主力资金数据")
        lines.append(f"⚠️ 主数据源降级至 {label}：按量价口径评分，{extra}，"
                     "分数不可与历史东财口径直接比较")
    lines.append("")
    for i, c in enumerate(pool[:TOP_N], 1):
        detail = (
            f"量比{c['vol_ratio']:.1f}  换手{c['turnover']:.1f}%  "
            f"主力{c['main_net_yi']:+.2f}亿({c['main_pct']:+.1f}%)  {c['industry']}"
            if has_fund else
            f"换手{c['turnover']:.1f}%  成交额{c['amount_yi']:.2f}亿  流通市值{c['float_mv_yi']:.0f}亿"
        )
        lines.append(
            f"{i}. {c['name']} {c['code']}  {c['score']}分/{c['grade']}  "
            f"{c['pct']:+.2f}%  {detail}"
        )
    lines += [
        "",
        ("板块共振：" + ("、".join(f"{s['name']}{s['count']}只" for s in overview["top_sectors"]) or "无明显共振"))
        if has_fund else f"板块共振：{label} 无可比行业数据，本期跳过",
        "",
        "⚠️ 盘中实时快照，午后可能变盘；以 15:30 收盘选股为定稿，不等于收盘信号。",
        ("数据来源：东方财富（多域名容灾）。" if has_fund
         else f"数据来源：{label}（降级源）。")
        + "仅供研究复盘，不构成投资建议。",
    ]
    return "\n".join(lines)


def push(payload: dict, fallback: str, webhook: str) -> bool:
    if not webhook:
        print("::warning::FEISHU_WEBHOOK 未配置，盘中观察池已生成但不会推送（请在仓库 Secrets 中配置）")
        print(fallback[:400])
        return True
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        if result.get("code") == 0 or result.get("StatusCode") == 0:
            print("✅ 飞书交互卡片推送成功")
            return True
        print(f"⚠️ 卡片推送被拒：{result}，降级为文本消息")
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 卡片推送异常：{type(exc).__name__} {exc}，降级为文本消息")

    text_body = json.dumps(
        {"msg_type": "text", "content": {"text": fallback}}, ensure_ascii=False
    ).encode("utf-8")
    try:
        req2 = urllib.request.Request(
            webhook, data=text_body,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with urllib.request.urlopen(req2, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        ok = result.get("code") == 0 or result.get("StatusCode") == 0
        print("✅ 文本消息推送成功" if ok else f"❌ 文本推送也被拒：{result}")
        return ok
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 文本推送异常：{type(exc).__name__} {exc}")
        return False


def emit_github_output(is_trading: bool) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    line = f"is_trading={'true' if is_trading else 'false'}"
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    print(line)


def main() -> int:
    ap = argparse.ArgumentParser(description="盘中选股观察池（13:00）")
    ap.add_argument("--dry-run", action="store_true", help="本地试跑，不推送飞书")
    ap.add_argument("--check-trading-day", action="store_true", help="仅检查交易日并输出给 GitHub Actions")
    ap.add_argument("--json", action="store_true", help="打印完整观察池 JSON（调试用）")
    args = ap.parse_args()

    ok, reason = check_trading_day()
    print(f"[交易日判断] {ok} · {reason}")
    if args.check_trading_day:
        emit_github_output(ok)
        return 0
    if not ok:
        print("非交易日，跳过盘中选股与推送")
        return 0

    rows, quote_ts, source, snap = fetch_market_snapshot()
    if not rows:
        print("❌ 未取到实时行情数据（全部数据源失效）")
        return 1
    pool, overview = build_pool(rows, source, snap)
    print(
        f"[数据源] {overview['source_label']} · 口径 {source} / 质量 {overview['quality']}"
        f"（{'完整资金' if overview['has_fund'] else '量价降级'}）"
    )
    for note in overview["notes"]:
        print(f"          · {note}")
    print(
        f"[扫描] 强势股 {overview['scanned']} 只 → 入池 {overview['qualified']} 只；"
        f"涨停约 {overview['limit_up']} 家；板块共振 "
        f"{', '.join(s['name'] + str(s['count']) for s in overview['top_sectors'][:3]) or '无'}"
    )
    for c in pool[:TOP_N]:
        extra = (f"主力{c['main_net_yi']:+.2f}亿  {c['industry']}"
                 if overview["has_fund"] else f"换手{c['turnover']:.1f}%  流通{c['float_mv_yi']:.0f}亿")
        print(
            f"  {c['score']:5.1f} {c['grade']}  {c['code']} {c['name']:<8} "
            f"{c['pct']:+6.2f}%  {extra}"
        )

    card = build_card(pool, overview, quote_ts)
    fallback = card_to_text(pool, overview, quote_ts)
    if args.json:
        print(json.dumps({"pool": pool[:20], "overview": overview}, ensure_ascii=False))
    if args.dry_run:
        print("\n--- 飞书卡片结构 ---")
        print(json.dumps(card, ensure_ascii=False)[:2000])
        print("\n--- 降级文本 ---")
        print(fallback[:1200])
        print("\n[dry-run] 未推送")
        return 0

    webhook = os.environ.get("FEISHU_WEBHOOK", "").strip()
    return 0 if push(card, fallback, webhook) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 盘中选股失败：{type(exc).__name__} {exc}")
        sys.exit(1)
