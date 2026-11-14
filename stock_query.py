#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""个股深度分析 → 飞书推送（图片卡片优先，失败退回文本）。

在 GitHub Actions 手动触发（填股票代码），在 runner 上跑真实数据取数
（行情 / 资金流 / 估值 / 财务），合成「策略信号 + 买入建议」，
默认渲染成**一张图**推到飞书群（长文本在手机上要滑很多屏）。

与 verify-valuation.yml 的分工：
  - verify-valuation.yml：给开发看的数据源自检，只打日志、不推送；
  - 本脚本：给群里看的结论，会真的推送。

用法：
    python stock_query.py 600519 000001
    STOCK_CODES=600519,000001 PERIOD=90d python stock_query.py

环境变量：
    FEISHU_APP_ID / FEISHU_APP_SECRET   自建应用凭证（发图片必需）
    FEISHU_CHAT_ID                     Worker 传下来的会话 id（哪问哪答）
    FEISHU_WEBHOOK                     群自定义机器人 webhook（纯文本兜底）
    CARD_DIR                           图片输出目录，默认 reports/cards
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

BJ_TZ = timezone(timedelta(hours=8))
FEISHU_API = "https://open.feishu.cn/open-apis"

# 单次最多查 6 只：再多会拉长运行时间并容易触发数据源限流
MAX_CODES = 6

# 腾讯实时行情：三个等价域名，任一可用即可（与 utils/stock_data_fetcher 同一套容灾思路）
TENCENT_HOSTS = ("https://qt.gtimg.cn", "https://sqt.gtimg.cn", "http://qt.gtimg.cn")


# ---------------------------------------------------------------- 小工具

def _is_dash(v) -> bool:
    return v is None or (isinstance(v, float) and v != v)


def fmt_yi(yuan, digits: int = 2) -> str:
    """元 → 亿元文本。"""
    if _is_dash(yuan) or not yuan:
        return "—"
    return f"{float(yuan) / 1e8:.{digits}f}亿"


def fmt_num(v, digits: int = 1, suffix: str = "") -> str:
    if _is_dash(v):
        return "—"
    return f"{float(v):.{digits}f}{suffix}"


def fmt_pct(v, digits: int = 2) -> str:
    """0.0196 → +1.96%"""
    if _is_dash(v):
        return "—"
    return f"{float(v) * 100:+.{digits}f}%"


def _clip(text: str, limit: int = 130) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------- 实时行情

def fetch_quotes(codes: list) -> dict:
    """腾讯批量取现价/涨跌幅/市值/换手（一次请求搞定所有代码，避免逐个查被限流）。

    字段位置（`~` 分割，腾讯行情协议）：3=现价 32=涨跌幅 38=换手率
    44=**流通**市值(亿) 45=**总**市值(亿)。

    Returns:
        {code: {"price", "pct", "cap"(总市值,元), "float_mv"(流通市值,元), "turnover"(%)}}
    """
    if not codes:
        return {}
    query = ",".join(
        f"{'sh' if c[0] in '68' else 'sz'}{c}" for c in codes
    )
    text = ""
    for host in TENCENT_HOSTS:
        try:
            resp = requests.get(f"{host}/q={query}", timeout=10, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
            })
            if resp.status_code == 200 and (resp.text or "").strip():
                text = resp.text
                break
        except Exception:  # noqa: BLE001  换下一个域名
            continue

    out = {}
    for line in text.strip().split(";"):
        if "v_" not in line or "~" not in line:
            continue
        try:
            parts = line.split("~")
            if len(parts) < 46:
                continue
            code = parts[2].strip()
            if not code:
                continue

            def _f(idx):
                raw = parts[idx].strip() if idx < len(parts) else ""
                return float(raw) if raw else None

            price, pct = _f(3), _f(32)
            turnover = _f(38)
            # 44 是流通市值、45 才是总市值：早先只读了 44 却当总市值显示，
            # 全流通股看不出差别，限售比例高的股会明显偏小
            fmv_yi, tmv_yi = _f(44), _f(45)
            out[code] = {
                "price": price if price and price > 0 else None,
                "pct": (pct / 100.0) if pct is not None else None,
                "cap": ((tmv_yi or fmv_yi) * 1e8) if (tmv_yi or fmv_yi) else None,
                "float_mv": (fmv_yi * 1e8) if fmv_yi else None,
                "turnover": turnover if turnover is not None else None,
            }
        except Exception:  # noqa: BLE001  单行解析失败不影响其它股票
            continue
    return out


# ---------------------------------------------------------------- 单只股票排版

def _color_icon(pct):
    """A 股习惯：红涨绿跌；涨跌幅未知时不打点。"""
    if _is_dash(pct):
        return "⚪"
    return "🔴" if pct >= 0 else "🟢"


def format_stock(index: int, code: str, result: dict, quote: dict,
                 chips: dict | None = None, flow_hist: list | None = None) -> str:
    """把单只股票的分析结果排成若干行纯文本。"""
    if not result:
        return f"{index}. {code}  ❌ 分析失败（数据源全部不可用或代码不存在）"

    info = result.get("stock_info") or {}
    technical = result.get("technical") or {}
    fund = result.get("fundamental") or {}
    flow = (result.get("fund_flow") or {}).get("flow_analysis") or {}
    advice = result.get("buy_advice") or {}
    signals = result.get("signals") or []

    name = info.get("name") or code
    market = info.get("market") or ""
    industry = info.get("industry") or "未知行业"
    if industry in ("未知行业", "未知"):
        industry = "行业未知（数据源未覆盖）"

    lines = [f"{index}. {name}（{code}）{market}".rstrip()]
    lines.append(f"   {industry}")

    price_line = []
    if quote.get("price"):
        price_line.append(f"{_color_icon(quote.get('pct'))} {quote['price']:.2f}")
    if not _is_dash(quote.get("pct")):
        price_line.append(fmt_pct(quote["pct"]))
    if quote.get("cap"):
        price_line.append(f"市值 {fmt_yi(quote['cap'])}")
    if price_line:
        lines.append("   " + "  ".join(price_line))

    if advice:
        lines.append(
            f"   📌 建议：{advice.get('action', '—')}"
            f"（{advice.get('score', '—')}/100）｜"
            f"看多 {advice.get('bull_count', 0)} · 看空 {advice.get('bear_count', 0)}"
        )

    ind = technical.get("indicators") or {}
    lines.append(
        "   📈 技术："
        f"{technical.get('trend', '未知')} ｜ MACD {ind.get('MACD', '—')} ｜ "
        f"KDJ {ind.get('KDJ', '—')} ｜ RSI {fmt_num(ind.get('RSI'))} ｜ "
        f"{ind.get('Bollinger', '—')}"
    )

    if flow:
        lines.append(
            f"   💰 资金：主力净流入 {fmt_yi(flow.get('main_inflow'))}"
            f"（{flow.get('direction', '—')}·{flow.get('strength', '—')}）"
        )

    rows = [r for r in (flow_hist or []) if r.get("net") is not None][-20:]
    if rows:
        net = sum(r["net"] for r in rows) / 1e8
        sup = sum((r.get("super_net") or 0) for r in rows) / 1e8
        up_days = sum(1 for r in rows if r["net"] > 0)
        lines.append(
            f"   💧 近{len(rows)}日资金：净流入 {net:+.2f} 亿"
            f"（流入 {up_days} 天 / 流出 {len(rows) - up_days} 天）｜ "
            f"超大单 {sup:+.2f} 亿 ｜ 新浪口径，与主力口径不等价"
        )

    if chips and chips.get("available"):
        avg = chips.get("avg_cost")
        dev = chips.get("deviation")
        lines.append(
            f"   🎯 筹码：平均成本 {fmt_num(avg, 2)}"
            f"（现价{'低' if (dev or 0) < 0 else '高'} {abs(dev or 0) * 100:.1f}%）｜ "
            f"获利 {fmt_num((chips.get('profit_ratio') or 0) * 100, 0, '%')}"
            f"（{chips.get('profit_level', '—')}）｜ "
            f"90% 成本 {fmt_num(chips.get('cost_low'), 2)}~{fmt_num(chips.get('cost_high'), 2)} ｜ "
            f"集中度 {fmt_num((chips.get('concentration') or 0) * 100, 1, '%')}"
            f"（{chips.get('concentration_level', '—')}）｜ "
            f"筹码峰 {fmt_num(chips.get('peak_price'), 2)}"
        )

    val = fund.get("valuation") or {}
    lines.append(
        f"   📊 估值：PE {fmt_num(val.get('pe'))}（{val.get('pe_level', '—')}）｜ "
        f"PB {fmt_num(val.get('pb'))}（{val.get('pb_level', '—')}）｜ "
        f"PS {fmt_num(val.get('ps'))}"
    )

    fin = fund.get("financial") or {}
    gr = fund.get("growth") or {}
    lines.append(
        f"   🧾 财务：ROE {fmt_num(fin.get('roe'))}% ｜ "
        f"负债率 {fmt_num(fin.get('debt_ratio'))}%（{fin.get('debt_level', '—')}）｜ "
        f"净利同比 {fmt_pct(gr.get('profit_growth'))}"
    )

    bull = [s.get("note") for s in signals if s.get("signal") == "看多" and s.get("note")]
    bear = [s.get("note") for s in signals if s.get("signal") == "看空" and s.get("note")]
    if bull:
        lines.append(f"   💡 看多：{_clip('；'.join(bull))}")
    if bear:
        lines.append(f"   ⚠️ 看空：{_clip('；'.join(bear))}")
    if not bull and not bear:
        lines.append("   ⚖️ 多空因素大致均衡，暂无明确方向")

    risks = [r for r in (advice.get("risks") or []) if r and "不构成" not in r and "未考虑突发" not in r]
    if risks:
        lines.append(f"   ❗ 风险：{_clip('；'.join(risks), 90)}")

    return "\n".join(lines)


# ---------------------------------------------------------------- 推送

def send_feishu(msg: str) -> bool:
    """推送到飞书群；未配置 webhook 时显式告警而不是假装成功。"""
    webhook = os.environ.get("FEISHU_WEBHOOK", "").strip()
    if not webhook:
        print("::warning::FEISHU_WEBHOOK 未配置，结论已生成但不会推送（请在仓库 Secrets 中配置）")
        return False
    try:
        resp = requests.post(
            webhook,
            json={"msg_type": "text", "content": {"text": msg}},
            timeout=15,
        )
        resp.raise_for_status()
        payload = resp.json()
        code = payload.get("code", payload.get("StatusCode"))
        if code not in (0, None):
            print(f"❌ 飞书返回错误：{payload}")
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 飞书推送异常：{type(exc).__name__} {exc}")
        return False


def _feishu_token():
    """用自建应用换 tenant_access_token；没配凭证返回 None。"""
    app_id = os.environ.get("FEISHU_APP_ID", "").strip()
    secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    if not app_id or not secret:
        return None
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": secret},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"tenant_access_token 失败 {data.get('code')} {data.get('msg')}")
    return data["tenant_access_token"]


def send_to_chat(msg: str, chat_id: str) -> bool:
    """用自建应用把结果发回「问这句话的那个会话」，群里问就回群里。

    群自定义机器人（webhook）只能往一个固定群发，做不到「哪问哪答」，
    所以由 Worker 把 chat_id 一起传下来。凭证缺失时返回 False，交给 webhook 兜底。
    """
    try:
        token = _feishu_token()
    except Exception as exc:  # noqa: BLE001
        print(f"::warning::取 tenant_access_token 失败：{exc}")
        return False
    if not token:
        print("::warning::未配置 FEISHU_APP_ID / FEISHU_APP_SECRET，无法按会话回复")
        return False
    try:
        resp = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": msg}, ensure_ascii=False),
            },
            timeout=15,
        )
        payload = resp.json()
        if payload.get("code") != 0:
            print(f"❌ 按会话发送失败：{payload.get('code')} {payload.get('msg')}")
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 按会话发送异常：{type(exc).__name__} {exc}")
        return False


def push(msg: str) -> bool:
    """优先「哪问哪答」；没拿到 chat_id 或发送失败就退回群 webhook。"""
    chat_id = os.environ.get("FEISHU_CHAT_ID", "").strip()
    if chat_id:
        if send_to_chat(msg, chat_id):
            print(f"[PUSH] 已发送到会话 {chat_id}")
            return True
        print("::warning::按会话发送失败，改用 FEISHU_WEBHOOK 兜底")
    return send_feishu(msg)


# ---------------------------------------------------------------- 图片推送

def upload_image(token: str, path) -> str:
    """上传 PNG 到飞书，拿 image_key（图片消息必需，webhook 发不了本地文件）。"""
    name = Path(path).name
    with open(path, "rb") as fp:
        resp = requests.post(
            f"{FEISHU_API}/im/v1/images",
            headers={"Authorization": f"Bearer {token}"},
            data={"image_type": "message"},
            files={"image": (name, fp, "image/png")},
            timeout=60,
        )
    payload = resp.json()
    if payload.get("code") != 0:
        code = payload.get("code")
        print(f"❌ 上传图片失败：{code} {payload.get('msg')}")
        if code in (99991672, 99991663):
            print("::warning::飞书应用缺少「获取与上传图片或文件资源 im:resource」权限，"
                  "开放平台 → 权限管理开通后需重新发布版本；本次退回文本推送")
        return ""
    key = (payload.get("data") or {}).get("image_key") or ""
    if not key:
        print(f"❌ 上传图片未返回 image_key：{payload}")
    return key


def send_image(token: str, image_key: str, chat_id: str) -> bool:
    try:
        resp = requests.post(
            f"{FEISHU_API}/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": chat_id,
                "msg_type": "image",
                "content": json.dumps({"image_key": image_key}),
            },
            timeout=20,
        )
        payload = resp.json()
        if payload.get("code") != 0:
            print(f"❌ 发送图片失败：{payload.get('code')} {payload.get('msg')}")
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 发送图片异常：{type(exc).__name__} {exc}")
        return False


def card_dir() -> Path:
    raw = (os.environ.get("CARD_DIR") or "").strip()
    out = Path(raw) if raw else ROOT / "reports" / "cards"
    out.mkdir(parents=True, exist_ok=True)
    return out


def push_cards(pairs: list, blocks: list, msg: str) -> bool:
    """逐只渲染图片并发到会话；任何一环不通就退回文本，绝不发半残内容。

    Args:
        pairs:  [(文本块下标, render_stock_card 的关键字参数)]
        blocks: 纯文本块列表（图片发失败时按同一只股票补发）
        msg:    全部股票的完整文本（整体兜底）
    """
    chat_id = os.environ.get("FEISHU_CHAT_ID", "").strip()
    try:
        token = _feishu_token()
    except Exception as exc:  # noqa: BLE001
        print(f"::warning::取 tenant_access_token 失败，改用文本：{exc}")
        token = None
    if not token or not chat_id:
        print("[CARD] 没有应用凭证或会话 id，跳过图片，直接发文本")
        return push(msg)

    # 延迟导入：没装 matplotlib 的环境（例如只跑取数）不该因此崩掉
    try:
        from utils.stock_card import render_stock_card
    except Exception as exc:  # noqa: BLE001
        print(f"::warning::图片模块不可用（{type(exc).__name__} {exc}），改用文本")
        return push(msg)

    outdir = card_dir()
    failed = []
    sent = 0
    for idx, spec in pairs:
        png = outdir / f"{spec['code']}_card.png"
        try:
            if not render_stock_card(png, **spec):
                failed.append(idx)
                continue
        except Exception as exc:  # noqa: BLE001
            print(f"::warning::{spec['code']} 渲染异常：{type(exc).__name__} {exc}")
            failed.append(idx)
            continue
        key = upload_image(token, png)
        if not key or not send_image(token, key, chat_id):
            failed.append(idx)
            continue
        sent += 1
        print(f"[CARD] {spec['code']} 已发送图片 {png}")

    if not sent:
        print("[CARD] 一张都没发出去，整段文本兜底")
        return push(msg)

    if failed:
        # 只补发没发出去的那几只，避免和已发出的图片重复刷屏
        tail = "\n\n".join(blocks[i] for i in sorted(set(failed)) if i < len(blocks))
        if tail.strip():
            print(f"[CARD] {len(set(failed))} 只图片失败，补发文本")
            send_to_chat("（以下因图片生成失败改用文字）\n\n" + tail, chat_id)
    print(f"[PUSH] 已发送 {sent}/{len(pairs)} 张图片到会话 {chat_id}")
    return True



def write_summary(msg: str) -> None:
    """同时写一份到 GitHub Actions 运行页的 Summary，方便不开飞书也能看。"""
    path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fp:
            fp.write("## 个股深度分析\n\n```\n" + msg + "\n```\n")
    except OSError as exc:
        print(f"::warning::写 Summary 失败：{exc}")


# ---------------------------------------------------------------- 主流程

def build_card_spec(code: str, analysis: dict, quote: dict, hist, generated_at: str,
                    chips: dict | None = None,
                    flow_hist: list | None = None) -> dict:
    """把分析结果摊平成 render_stock_card 的关键字参数（字段缺失一律给空值，别让渲染崩）。"""
    if not analysis:
        return {}
    info = analysis.get("stock_info") or {}
    fundamental = analysis.get("fundamental") or {}
    industry = info.get("industry") or ""
    if industry in ("未知行业", "未知"):
        industry = "行业未知"
    return {
        "code": code,
        "name": info.get("name") or code,
        "market": info.get("market") or "",
        "industry": industry,
        "price": quote.get("price"),
        "pct": quote.get("pct"),
        "cap": quote.get("cap"),
        "technical": analysis.get("technical") or {},
        "flow": (analysis.get("fund_flow") or {}).get("flow_analysis") or {},
        "valuation": fundamental.get("valuation") or {},
        "financial": fundamental.get("financial") or {},
        "growth": fundamental.get("growth") or {},
        "advice": analysis.get("buy_advice") or {},
        "signals": analysis.get("signals") or [],
        "hist": hist,
        "chips": chips or {},
        "flow_hist": flow_hist or [],
        "generated_at": generated_at,
    }


def parse_codes(argv: list) -> list:
    raw = " ".join(argv) if argv else (os.environ.get("STOCK_CODES") or "")
    codes, bad = [], []
    seen = set()
    for token in raw.replace("，", ",").replace(" ", ",").split(","):
        token = token.strip()
        if not token:
            continue
        if not (token.isdigit() and len(token) == 6):
            bad.append(token)
            continue
        if token not in seen:
            seen.add(token)
            codes.append(token)
    if bad:
        print(f"::warning::忽略非法代码：{bad}（需 6 位数字）")
    return codes


def main() -> int:
    codes = parse_codes(sys.argv[1:])
    period = (os.environ.get("PERIOD") or "90d").strip() or "90d"

    if not codes:
        print("::error::没有可查询的股票代码")
        return 1
    if len(codes) > MAX_CODES:
        print(f"::warning::一次最多查 {MAX_CODES} 只，已截断（收到 {len(codes)} 只）")
        codes = codes[:MAX_CODES]

    print(f"[QUERY] codes={codes} period={period}")

    # 延迟导入：装依赖失败的报错更清晰，也不会因为本地缺包直接语法错误
    from stock_analyzer import StockAnalyzer
    from stock_analyzer.advisor import build_strategy_analysis, build_buy_advice
    from stock_analyzer.real_fundamentals import (
        enrich_fundamental, get_real_financials, get_real_valuation,
    )
    from utils.chip_analysis import compute_chip_distribution, float_shares_from_mv
    from utils.market_sources import sina_fetch_flow_history

    quotes = fetch_quotes(codes)
    analyzer = StockAnalyzer()
    now = datetime.now(BJ_TZ)

    blocks, pairs, ok = [], [], 0
    for idx, code in enumerate(codes, 1):
        try:
            analysis = analyzer.analyze(code, period=period)
        except Exception as exc:  # noqa: BLE001  单只失败不影响其它
            print(f"::warning::{code} 分析异常：{type(exc).__name__} {exc}")
            analysis = None

        if not analysis:
            blocks.append(format_stock(idx, code, None, {}))
            continue

        try:
            analysis["fundamental"] = enrich_fundamental(code, analysis.get("fundamental"))
        except Exception as exc:  # noqa: BLE001  真实财务取不到就用原值
            print(f"::warning::{code} 真实财务补全失败：{type(exc).__name__} {exc}")

        try:
            analysis["signals"] = build_strategy_analysis(analysis)
            analysis["buy_advice"] = build_buy_advice(
                analysis, get_real_valuation(code), get_real_financials(code)
            )
        except Exception as exc:  # noqa: BLE001
            print(f"::warning::{code} 建议合成失败：{type(exc).__name__} {exc}")
            analysis.setdefault("signals", [])
            analysis.setdefault("buy_advice", {})

        quote = quotes.get(code, {})
        # 腾讯行情优先；拿不到就用库里市值兜底，别让市值一栏空着
        if not quote.get("cap"):
            quote["cap"] = ((analysis.get("fundamental") or {}).get("valuation") or {}).get("market_cap")

        # 画 K 线要历史行情；拿不到就只画指标（卡片照样能出，不至于整只退文本）
        hist = None
        try:
            hist = analyzer.data_fetcher.get_stock_quote(code, period="200d")
        except Exception as exc:  # noqa: BLE001
            print(f"::warning::{code} 取 K 线数据失败，图片不带走势图：{type(exc).__name__} {exc}")

        # 筹码分布：需要「流通股本」才成立（换手率 = 成交量 / 流通股本），
        # 拿不到就整块跳过，绝不猜一个股本糊个数出来
        chips = {}
        if hist is not None and len(hist):
            try:
                last_close = float(hist["close"].iloc[-1])
                shares = float_shares_from_mv(
                    quote.get("float_mv"), quote.get("price") or last_close
                )
                chips = compute_chip_distribution(
                    hist, shares, price=quote.get("price") or last_close
                )
                if chips.get("available"):
                    print(f"[CHIP] {code} 平均成本 {chips['avg_cost']:.2f} "
                          f"获利 {chips['profit_ratio']:.0%} "
                          f"集中度 {chips['concentration']:.1%}")
                else:
                    print(f"::warning::{code} 筹码分布跳过：{chips.get('reason')}")
            except Exception as exc:  # noqa: BLE001  筹码算不出来不影响主结论
                print(f"::warning::{code} 筹码计算出错：{type(exc).__name__} {exc}")
                chips = {}

        # 近一个月资金流入流出：新浪历史接口按天给净额，一次请求够画 20 根柱子
        flow_hist: list = []
        try:
            flow_hist = sina_fetch_flow_history(code, days=25)
            if flow_hist:
                print(f"[FLOW] {code} 近 {len(flow_hist)} 日资金流已取到")
        except Exception as exc:  # noqa: BLE001  资金流只是补充，失败不影响主结论
            print(f"::warning::{code} 资金流历史取不到：{type(exc).__name__} {exc}")

        block_index = len(blocks)
        blocks.append(format_stock(idx, code, analysis, quote, chips, flow_hist))
        spec = build_card_spec(code, analysis, quote, hist, now.strftime("%Y-%m-%d %H:%M"),
                               chips, flow_hist)
        if spec:
            pairs.append((block_index, spec))
        ok += 1
        print(f"[OK] {code} {analysis['stock_info'].get('name')} "
              f"建议={analysis['buy_advice'].get('action')} "
              f"score={analysis['buy_advice'].get('score')}")

    header = [
        f"📊 个股深度分析 · {now.strftime('%Y-%m-%d %H:%M')}",
        "",
    ]
    footer = [
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "数据来源：腾讯行情 / 同花顺资金流 / 百度估值 / 新浪财报",
        "⚠️ 量化模型基于公开历史数据的参考结论，不构成投资建议。",
    ]
    msg = "\n".join(header + blocks) + "\n" + "\n".join(footer)

    print("---- 消息预览 ----")
    print(msg)
    print("------------------")

    write_summary(msg)
    sent = push_cards(pairs, blocks, msg)
    print(f"[DONE] {ok}/{len(codes)} 只有结果，推送={'成功' if sent else '未发送'}")

    # 一只都没成功，或者明明配了推送通道却发不出去 —— 都要让 Actions 红，避免假绿
    if ok == 0:
        print("::error::全部股票分析失败")
        return 1
    configured = any(
        os.environ.get(k, "").strip() for k in ("FEISHU_CHAT_ID", "FEISHU_WEBHOOK")
    )
    if configured and not sent:
        print("::error::飞书推送失败")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
