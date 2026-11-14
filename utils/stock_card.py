#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把个股分析结果渲染成一张可直接发到飞书的图片卡片。

为什么单独一个模块：飞书群里的长文本很难读，手机上要滑很多屏。图片把
「名称/价格/涨跌幅 + K 线 + 关键指标 + 结论」压进一屏，扫一眼就知道结论。

约定
----
- A 股配色：**红涨绿跌**，与飞书/微信里的习惯一致（不用欧美那套）。
- 中文字体找不到时 `render_stock_card()` 返回 False，调用方退回纯文本，
  绝不发出「豆腐块」乱码图。
- 只依赖 matplotlib（不引入 mplfinance），K 线用矩形 + 影线手绘。
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

import matplotlib

matplotlib.use("Agg")  # 必须在 pyplot 之前，runner 上没有显示设备

import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import Rectangle

# ---------------------------------------------------------------- 配色

UP = "#d93a3a"          # 涨（红）
DOWN = "#12a150"        # 跌（绿）
FLAT = "#8a8f99"
INK = "#1b1f24"         # 主文字
SUB = "#6b7280"         # 次要文字
LINE = "#e6e8eb"        # 分隔线
PANEL = "#f6f7f9"       # 浅灰底
BG = "#ffffff"

# 均线：短期（5/10）看节奏，中期（20）看趋势，长期（60）看多空分界。
# 颜色避开红绿（那两位已经被涨跌占用了），用橙/紫/蓝/深灰区分。
MAS = (
    (5, "#f0a63d"),
    (10, "#a855f7"),
    (20, "#3b82f6"),
    (60, "#374151"),
)
KLINE_BARS = 120  # 画多少根 K 线；MA60 需要至少 60 根，90 根时开头会缺一大截
FLOW_BARS = 20    # 资金流画多少个交易日（一个月约 20 个交易日）

# 筹码分布用的浅色（比涨跌色淡，避免和 K 线抢眼）：成本在现价下方=浮盈，上方=套牢
CHIP_PROFIT = "#eaa1a1"
CHIP_TRAPPED = "#8ec4a8"
CHIP_AVG = "#8a5cf5"

FONT_FILES = (
    # Linux runner（workflow 里 apt-get install fonts-noto-cjk 后就有）
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    # Windows（本地预览）
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
)

FONT_FAMILIES = (
    "Noto Sans CJK SC", "Noto Sans CJK JP", "Noto Sans SC",
    "Source Han Sans SC", "Microsoft YaHei", "SimHei",
    "WenQuanYi Zen Hei", "PingFang SC",
)

_BOLD_FILES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
)

_font_name: Optional[str] = None
_font_bold: Optional[str] = None
_font_ready = False


def setup_font() -> Optional[str]:
    """挑一个能显示中文的字体并注册给 matplotlib；找不到返回 None。"""
    global _font_name, _font_bold, _font_ready
    if _font_ready:
        return _font_name
    _font_ready = True

    def _register(path: str) -> Optional[str]:
        try:
            if Path(path).is_file():
                font_manager.fontManager.addfont(path)
                return font_manager.FontProperties(fname=path).get_name()
        except Exception:  # noqa: BLE001  字体坏了不影响别的候选
            return None
        return None

    for path in FONT_FILES:
        name = _register(path)
        if name:
            _font_name = name
            break

    if not _font_name:
        installed = {f.name for f in font_manager.fontManager.ttflist}
        for name in FONT_FAMILIES:
            if name in installed:
                _font_name = name
                break

    for path in _BOLD_FILES:
        name = _register(path)
        if name:
            _font_bold = name
            break
    if not _font_bold:
        _font_bold = _font_name

    if _font_name:
        plt.rcParams["font.family"] = _font_name
        plt.rcParams["axes.unicode_minus"] = False  # 负号不要变方块
    return _font_name


# ---------------------------------------------------------------- 小工具

def _cjk_len(text: str) -> int:
    """估算显示宽度：中日韩全角算 2 列，其它算 1 列。"""
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)


def _wrap(text: str, cols: int, max_lines: int = 3) -> list:
    """按显示宽度折行，超出 max_lines 用省略号收尾。

    额外处理「行首标点」：中文里让 `；` `，` 落在行首很丑，遇到就把它挪回上一行末尾
    （每行都留了余量，多一个字符不会撑出边界）。
    """
    text = (text or "").strip()
    if not text:
        return []
    lines, cur, width = [], "", 0
    for ch in text:
        w = 2 if ord(ch) > 0x2E7F else 1
        if width + w > cols and cur:
            lines.append(cur)
            cur, width = "", 0
            if len(lines) >= max_lines:
                break
        cur += ch
        width += w
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines and _cjk_len("".join(lines)) < _cjk_len(text):
        lines[-1] = lines[-1][:-1] + "…"

    leading_punct = "，。；：、！？）」』"
    for i in range(1, len(lines)):
        while lines[i] and lines[i][0] in leading_punct:
            lines[i - 1] += lines[i][0]
            lines[i] = lines[i][1:]
    return [line for line in lines if line]



def _is_dash(v) -> bool:
    return v is None or (isinstance(v, float) and v != v) or v == ""


def fmt_yi(yuan, digits: int = 2) -> str:
    if _is_dash(yuan) or not yuan:
        return "—"
    return f"{float(yuan) / 1e8:.{digits}f}亿"


def fmt_num(v, digits: int = 1, suffix: str = "") -> str:
    if _is_dash(v):
        return "—"
    try:
        return f"{float(v):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return "—"


def fmt_pct(v, digits: int = 2) -> str:
    if _is_dash(v):
        return "—"
    try:
        return f"{float(v) * 100:+.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def _color_for(pct) -> str:
    if _is_dash(pct):
        return FLAT
    return UP if float(pct) >= 0 else DOWN


# ---------------------------------------------------------------- 各区块绘制

def _draw_header(fig, *, name, code, market, industry, price, pct, cap):
    """顶部色带：左名称+代码+行业，右价格+涨跌幅+市值。"""
    band = fig.add_axes([0, 0.885, 1, 0.115])
    band.set_axis_off()
    color = _color_for(pct)
    band.add_patch(Rectangle((0, 0), 1, 1, transform=band.transAxes,
                            facecolor=color, edgecolor="none"))

    bold = _font_bold or _font_name
    band.text(0.045, 0.70, name, transform=band.transAxes, color="white",
              fontsize=25, fontweight="bold", family=bold, va="center")
    sub = " · ".join(x for x in (code, market, industry) if x)
    band.text(0.045, 0.30, sub, transform=band.transAxes, color="white",
              alpha=0.92, fontsize=12.5, va="center", family=bold)

    if not _is_dash(price):
        band.text(0.955, 0.62, f"{float(price):.2f}", transform=band.transAxes,
                  color="white", fontsize=30, fontweight="bold",
                  family=bold, ha="right", va="center")
    extra = "   ".join(x for x in (fmt_pct(pct) if not _is_dash(pct) else "",
                                   f"市值 {fmt_yi(cap)}" if cap else "") if x)
    if extra:
        band.text(0.955, 0.24, extra, transform=band.transAxes, color="white",
                  alpha=0.95, fontsize=12.5, ha="right", va="center", family=bold)


def _draw_kline(fig, hist, name, rect, vol_rect=None):
    """K 线 + MA5/MA10/MA20/MA60 + 成交量。

    四条均线是看盘的基本盘：短期（5/10）看节奏，中期（20）看趋势，长期（60）
    看多空分界。图例里直接带上当前值，比只写「MA20」有用得多。

    hist 为空时只画一条窄提示条（不占整块图高，否则卡片中间会空一大片）。
    """
    if not vol_rect:
        ax = fig.add_axes(rect)
        ax.set_axis_off()
        ax.add_patch(Rectangle((0, 0.1), 1, 0.8, transform=ax.transAxes,
                               facecolor=PANEL, edgecolor="none"))
        ax.text(0.5, 0.5, "行情历史数据暂不可用（数据源超时），下方仍为实时指标",
                transform=ax.transAxes, ha="center", va="center",
                fontsize=11, color=SUB, family=_font_name)
        return None

    price_ax = fig.add_axes(rect)
    vol_ax = fig.add_axes(vol_rect, sharex=price_ax)

    if hist is None or len(hist) < 5:
        price_ax.set_axis_off()
        price_ax.text(0.5, 0.5, "行情历史数据暂不可用", transform=price_ax.transAxes,
                      ha="center", va="center", fontsize=13, color=SUB,
                      family=_font_name)
        vol_ax.set_axis_off()
        return None

    # 120 根才够 MA60 画出像样的曲线（tail(90) 时 MA60 只有 31 个点，开头一节是空的）
    df = hist.tail(KLINE_BARS).reset_index(drop=True)
    opens = [float(x) for x in df["open"]]
    closes = [float(x) for x in df["close"]]
    highs = [float(x) for x in df["high"]]
    lows = [float(x) for x in df["low"]]
    vols = [float(x) for x in df.get("volume", [0] * len(df))]
    xs = list(range(len(df)))

    for i, (o, c, h, l) in enumerate(zip(opens, closes, highs, lows)):
        color = UP if c >= o else DOWN
        # 影线
        price_ax.plot([i, i], [l, h], color=color, linewidth=0.8, solid_capstyle="butt")
        # 实体：一字板时给个最小高度，否则线看不见
        low, high = min(o, c), max(o, c)
        height = max(high - low, (max(highs) - min(lows)) * 0.0015)
        price_ax.add_patch(Rectangle((i - 0.32, low), 0.64, height,
                                     facecolor=color, edgecolor=color, linewidth=0))

    def _ma(values, window):
        out = []
        for i in range(len(values)):
            if i + 1 < window:
                out.append(float("nan"))
            else:
                out.append(sum(values[i + 1 - window:i + 1]) / window)
        return out

    for window, color in MAS:
        if len(df) < window:  # 数据不够这条均线就不画，避免半截线误导
            continue
        series = _ma(closes, window)
        last = series[-1]
        price_ax.plot(xs, series, color=color, linewidth=1.2,
                      label=f"MA{window} {last:.2f}")

    price_ax.set_xlim(-1, len(df))
    price_ax.grid(axis="y", color=LINE, linewidth=0.7)
    price_ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        price_ax.spines[side].set_visible(False)
    price_ax.spines["bottom"].set_color(LINE)
    price_ax.tick_params(axis="y", labelsize=9, colors=SUB, length=0)
    # x 轴日期刻度画在成交量轴上（最底下），画在上面会和「成交量」标题挤在一起
    price_ax.tick_params(axis="x", labelbottom=False, length=0)
    # 四条均线放在图外顶部（右对齐），不压在 K 线上；左侧标题写「近 N 个交易日」
    price_ax.legend(loc="lower right", bbox_to_anchor=(1, 1.005), ncol=len(MAS),
                    fontsize=8.5, frameon=False, borderaxespad=0,
                    handlelength=1.5, columnspacing=1.2, handletextpad=0.5,
                    prop={"family": _font_name, "size": 8.5})
    price_ax.set_title(f"{name} 近 {len(df)} 个交易日", loc="left", fontsize=11.5,
                       color=SUB, family=_font_name, pad=6)

    vol_colors = [UP if c >= o else DOWN for o, c in zip(opens, closes)]
    # 成交量按量级换单位：原始股数动辄 8~9 位，y 轴会挤成 1e8 看不清
    vmax = max(vols) if vols else 0
    if vmax >= 1e8:
        unit, div = "亿", 1e8
    elif vmax >= 1e4:
        unit, div = "万", 1e4
    else:
        unit, div = "", 1
    vol_ax.bar(xs, [v / div for v in vols], color=vol_colors, width=0.64, linewidth=0)
    vol_ax.set_xlim(-1, len(df))
    vol_ax.grid(axis="y", color=LINE, linewidth=0.6)
    vol_ax.set_axisbelow(True)
    vol_ax.set_title(f"成交量（{unit}股）" if unit else "成交量（股）", loc="left",
                     fontsize=8, color=SUB, family=_font_name, pad=3)
    # 只留 0 和一个上限刻度：这块本来就只有 0.7 英寸高，三个刻度会和标题打架
    try:
        from matplotlib.ticker import MaxNLocator

        vol_ax.yaxis.set_major_locator(MaxNLocator(nbins=2, integer=False))
    except Exception:  # noqa: BLE001  locator 不可用就用默认刻度
        pass
    if "date" in df.columns:
        marks = [0, len(df) // 3, (len(df) * 2) // 3, len(df) - 1]
        try:
            labels = [str(df["date"].iloc[i])[5:10].replace("-", "/") for i in marks]
            vol_ax.set_xticks(marks)
            vol_ax.set_xticklabels(labels, fontsize=8, color=SUB, family=_font_name)
            vol_ax.tick_params(axis="x", length=0, pad=2, labelbottom=True)
            # 首尾两个日期默认居中，会半个字露到画布外（还会压住 y 轴的 0）
            tick_labels = vol_ax.get_xticklabels()
            if tick_labels:
                tick_labels[0].set_ha("left")
                tick_labels[-1].set_ha("right")
        except Exception:  # noqa: BLE001  日期格式异常就不标，不影响图
            vol_ax.set_xticklabels([])
    for side in ("top", "right", "left"):
        vol_ax.spines[side].set_visible(False)
    vol_ax.spines["bottom"].set_color(LINE)
    vol_ax.tick_params(axis="y", labelsize=7.5, colors=SUB, length=0)
    return price_ax


def _draw_chips(fig, chips, rect, price_ax):
    """K 线右侧的筹码分布：横条按价格排，长度=该价位的筹码占比。

    和价格轴共享 y 轴，所以一根横条正对着同价位的 K 线 —— 一眼能看出套牢盘
    压在哪、现价上下各有多少筹码。成本低于现价（浮盈）画红，高于现价（套牢）
    画绿，与 A 股涨跌配色一致。
    """
    centers = list((chips or {}).get("centers") or [])
    weights = list((chips or {}).get("weights") or [])
    if not centers or not weights or price_ax is None:
        return

    ax = fig.add_axes(rect, sharey=price_ax)
    now = chips.get("price") or centers[-1]
    step = (centers[1] - centers[0]) if len(centers) > 1 else 0.01
    colors = [CHIP_PROFIT if p <= now else CHIP_TRAPPED for p in centers]
    ax.barh(centers, weights, height=step * 0.86, color=colors, linewidth=0)

    avg = chips.get("avg_cost")
    if avg:
        ax.axhline(avg, color=CHIP_AVG, linewidth=1.1, linestyle=(0, (4, 2)))
    ax.set_xlim(0, 1.06)
    ax.set_ylim(price_ax.get_ylim())
    ax.set_axisbelow(True)
    # loc="right"：均线图例已经占满左侧那一行的右端，标题靠右才不粘连
    ax.set_title("筹码", loc="right", fontsize=8, color=SUB,
                 family=_font_name, pad=6)
    ax.tick_params(left=False, bottom=False, labelleft=False, labelbottom=False)
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)
    # 平均成本标在虚线右端，压白底方块避免和筹码条糊在一起
    if avg:
        ax.text(0.99, avg, f"{avg:.2f}", fontsize=6.5, color=CHIP_AVG,
                family=_font_name, ha="right", va="bottom",
                bbox={"boxstyle": "round,pad=0.15", "facecolor": "white",
                      "edgecolor": "none", "alpha": 0.85})


def _draw_flow(fig, flow_hist, rect):
    """近 N 个交易日资金净流入柱状图：红=净流入、绿=净流出，零轴居中。

    数据是新浪「资金流向历史」的 net（主动买入 − 主动卖出，含中小单）。它和
    东财「主力 = 大单 + 超大单」不是一个口径，所以标题右侧直接写「新浪口径」，
    别让人拿它跟卡片上的「主力净流入」对着抠。

    这块只有 0.95 英寸高，所以：x 轴只标首尾两个日期，y 轴只留 2 个刻度，
    柱宽 0.62 —— 再密就成一坨色块了。
    """
    ax = fig.add_axes(rect)
    rows = list(flow_hist or [])[-FLOW_BARS:]
    if not rows:
        ax.set_axis_off()
        return

    xs = list(range(len(rows)))
    vals = [(r.get("net") or 0) / 1e8 for r in rows]
    ax.bar(xs, vals, color=[UP if v >= 0 else DOWN for v in vals],
           width=0.62, linewidth=0)
    ax.axhline(0, color="#c9ced6", linewidth=0.9, zorder=3)
    ax.set_xlim(-0.8, len(rows) - 0.2)
    ax.grid(axis="y", color=LINE, linewidth=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)
    ax.tick_params(axis="y", labelsize=7.5, colors=SUB, length=0)
    # 只留「最低 / 0 / 最高」三个刻度：自动 locator 常常只给 0 和 10，
    # 负值那一侧就没有参照物了
    lo, hi = min(vals), max(vals)
    ticks = sorted({round(lo, 1), 0.0, round(hi, 1)})
    ax.set_yticks(ticks)
    ax.set_yticklabels([f"{t:g}" for t in ticks], fontsize=7.5, color=SUB,
                       family=_font_name)

    # 极端那天（比如涨停放量 +14 亿）会一枝独秀：数值交给顶部刻度，
    # 不再往柱子上压字——柱顶就是坐标轴顶边，写字必和右侧摘要撞行

    ax.set_xticks([0, len(rows) - 1])
    ax.set_xticklabels([rows[0]["date"][5:].replace("-", "/"),
                        rows[-1]["date"][5:].replace("-", "/")],
                       fontsize=7.5, color=SUB, family=_font_name)
    ax.tick_params(axis="x", length=0, pad=1)
    labels = ax.get_xticklabels()
    if labels:  # 首尾标签左/右对齐，别半个字露出画布
        labels[0].set_ha("left")
        labels[-1].set_ha("right")

    ax.set_title(f"近 {len(rows)} 交易日 资金净流入（亿元）", loc="left",
                 fontsize=8, color=SUB, family=_font_name, pad=4)
    total = sum(vals)
    sup = sum((r.get("super_net") or 0) for r in rows) / 1e8
    inflow = sum(1 for v in vals if v > 0)
    ax.text(1.0, 1.02,
            f"累计 {total:+.2f} 亿 · 流入 {inflow}/{len(rows)} 天 · 超大单 {sup:+.2f} 亿 · 新浪口径",
            transform=ax.transAxes, ha="right", va="bottom", fontsize=7.5,
            color=SUB, family=_font_name)


def _draw_metrics(fig, items, rect):
    """指标网格：items 是 [(标签, 值, 颜色)]，每行 4 个。

    行高有上限（约 1.4 英寸）：没有 K 线时可用高度会变多，若让 3 行铺满整块，
    标签和数字会被拉得很散。超出部分留白居中，视觉上就是正常留白。
    """
    ax = fig.add_axes(rect)
    ax.set_axis_off()

    cols = 4
    rows = max(1, math.ceil(len(items) / cols))
    # rect[3] 是这块在本图里的高度占比；换算成「每行最多占图高 0.115」
    max_row = (0.115 / rect[3]) if rect[3] else 1.0
    row_h = min(1 / rows, max_row)
    used = row_h * rows
    top = (1 - used) / 2  # 面板在 rect 内垂直居中
    ax.add_patch(Rectangle((0, top), 1, used, transform=ax.transAxes,
                           facecolor=PANEL, edgecolor="none", zorder=0))

    cell_w = 1 / cols
    gap = min(row_h * 0.20, 0.042)
    for idx, (label, value, color) in enumerate(items):
        r, c = divmod(idx, cols)
        x = c * cell_w + 0.022
        center = top + used - (r + 0.5) * row_h
        ax.text(x, center + gap, label, transform=ax.transAxes,
                fontsize=9.5, color=SUB, family=_font_name, va="center")
        ax.text(x, center - gap, value, transform=ax.transAxes,
                fontsize=12.5, color=color or INK, fontweight="bold",
                family=_font_bold or _font_name, va="center")


def _draw_advice(fig, advice, signals, risks, rect):
    """结论区：左边评级徽章，右边「看多 / 看空 / 风险」三块自上而下堆叠。

    右侧用游标逐行下移（va="top"），行高固定，避免不同长度的文案互相压字
    —— 早一版按固定 y 坐标画，两行以上的看多文案就会和看空行重叠。
    """
    ax = fig.add_axes(rect)
    ax.set_axis_off()

    action = (advice or {}).get("action") or "—"
    score = (advice or {}).get("score")
    bull = (advice or {}).get("bull_count") or 0
    bear = (advice or {}).get("bear_count") or 0

    tone = FLAT
    for kw, color in (("买", UP), ("增持", UP), ("持有", FLAT), ("减", DOWN), ("卖", DOWN)):
        if kw in action:
            tone = color
            break
    if "回避" in action or "观望" in action:
        tone = FLAT

    # 左侧：结论竖条 + 评级 + 分数 + 多空计数
    ax.add_patch(Rectangle((0, 0.06), 0.010, 0.88, transform=ax.transAxes,
                           facecolor=tone, edgecolor="none"))
    ax.text(0.028, 0.90, "综合结论", transform=ax.transAxes, fontsize=9.5,
            color=SUB, family=_font_name, va="top")
    ax.text(0.028, 0.70, action, transform=ax.transAxes, fontsize=17,
            color=tone, fontweight="bold", family=_font_bold or _font_name, va="top")
    if not _is_dash(score):
        ax.text(0.028, 0.50, f"{score}/100 分", transform=ax.transAxes, fontsize=11,
                color=INK, family=_font_bold or _font_name, va="top")
    ax.text(0.028, 0.32, f"看多 {bull} · 看空 {bear}", transform=ax.transAxes,
            fontsize=9.5, color=SUB, family=_font_name, va="top")

    # 竖分隔线 + 右侧三块文案
    ax.plot([0.335, 0.335], [0.08, 0.94], color=LINE, linewidth=1,
            transform=ax.transAxes, clip_on=False)

    bull_notes = [_n for _n in (s.get("note") for s in (signals or []) if s.get("signal") == "看多") if _n]
    bear_notes = [_n for _n in (s.get("note") for s in (signals or []) if s.get("signal") == "看空") if _n]

    cursor = 0.94
    for label, notes, color in (("看多", bull_notes, UP), ("看空", bear_notes, DOWN), ("风险", risks, SUB)):
        if not notes:
            continue
        # 宽度按实测折算：右栏 0.62 图宽 ≈ 84 个半角字符，留一点余量
        for i, line in enumerate(_wrap(f"{label}：" + "；".join(notes), 72, max_lines=2)):
            ax.text(0.365, cursor, line, transform=ax.transAxes, fontsize=9.5,
                    color=color if i == 0 else SUB, family=_font_name, va="top")
            cursor -= 0.105
        cursor -= 0.05



def _draw_footer(fig, sources, generated_at):
    fig.text(0.05, 0.030, f"数据来源：{sources}", fontsize=8.5, color=SUB,
             family=_font_name, va="center")
    fig.text(0.05, 0.012, "量化模型基于公开历史数据的参考结论，不构成投资建议。",
             fontsize=8.5, color=SUB, family=_font_name, va="center")
    if generated_at:
        fig.text(0.955, 0.030, generated_at, fontsize=8.5, color=SUB,
                 family=_font_name, ha="right", va="center")


# ---------------------------------------------------------------- 对外入口

def render_stock_card(
    path,
    *,
    code: str,
    name: str,
    market: str = "",
    industry: str = "",
    price=None,
    pct=None,
    cap=None,
    technical: Optional[dict] = None,
    flow: Optional[dict] = None,
    valuation: Optional[dict] = None,
    financial: Optional[dict] = None,
    growth: Optional[dict] = None,
    advice: Optional[dict] = None,
    signals: Optional[list] = None,
    hist=None,
    chips: Optional[dict] = None,
    flow_hist: Optional[list] = None,
    generated_at: str = "",
    sources: str = "腾讯行情 / 同花顺资金流 / 新浪资金流历史 / 百度估值 / 新浪财报",
) -> bool:
    """渲染一张个股卡片 PNG。

    Args:
        hist: 历史行情 DataFrame（列：date/open/high/low/close/volume），可给 None。
        chips: ``utils.chip_analysis.compute_chip_distribution`` 的返回值；
            没有（或缺流通股本导致 available=False）就不画筹码，卡片其余部分照旧。
        flow_hist: ``utils.market_sources.sina_fetch_flow_history`` 的返回值
            （[{date, net, super_net}]）；没有就不画资金流面板，图也相应变矮。

    Returns:
        True 渲染成功；False 表示环境缺中文字体或绘制失败，调用方应退回纯文本。
    """
    if not setup_font():
        print("::warning::没找到中文字体，跳过图片改用文本（runner 需 apt-get install fonts-noto-cjk）")
        return False

    technical = technical or {}
    flow = flow or {}
    valuation = valuation or {}
    financial = financial or {}
    growth = growth or {}
    advice = advice or {}
    signals = signals or []
    chips = chips if isinstance(chips, dict) and chips.get("available") else {}
    flow_hist = [r for r in (flow_hist or []) if isinstance(r, dict) and r.get("net") is not None]
    indicators = technical.get("indicators") or {}

    def _with_level(number, level, unit=""):
        """数值 + 档位；数值缺失时只留一个「—」，不要出现「—%（—）」这种残句。"""
        if number == "—":
            return "—"
        level = (level or "").strip()
        return f"{number}{unit}（{level}）" if level and level != "—" else f"{number}{unit}"

    items = [
        ("总市值", fmt_yi(cap), None),
        ("PE(TTM)", _with_level(fmt_num(valuation.get("pe")), valuation.get("pe_level")), None),
        ("PB", _with_level(fmt_num(valuation.get("pb")), valuation.get("pb_level")), None),
        ("PS", fmt_num(valuation.get("ps")), None),
        ("ROE", fmt_num(financial.get("roe"), suffix="%"), None),
        ("负债率", _with_level(fmt_num(financial.get("debt_ratio")),
                             financial.get("debt_level"), "%"), None),
        ("净利同比", fmt_pct(growth.get("profit_growth")),
         _color_for(growth.get("profit_growth")) if not _is_dash(growth.get("profit_growth")) else None),
        ("主力净流入", fmt_yi(flow.get("main_inflow")),
         _color_for(flow.get("main_inflow")) if not _is_dash(flow.get("main_inflow")) else None),
        ("趋势", technical.get("trend", "未知"), None),
        ("MACD", indicators.get("MACD", "—"), None),
        ("KDJ", indicators.get("KDJ", "—"), None),
        ("RSI", fmt_num(indicators.get("RSI")), None),
    ]

    # 筹码四项排在最后一行：回答「成本价多少 / 有多少人浮盈 / 套牢盘压在哪」
    if chips:
        avg, dev = chips.get("avg_cost"), chips.get("deviation")
        profit = chips.get("profit_ratio")
        conc = chips.get("concentration")
        items += [
            # 「低 5.4%」比「-5.4%」好读：一眼知道是现价低于成本还是高于成本
            ("平均成本",
             f"{avg:.2f}（{'低' if dev < 0 else '高'} {abs(dev):.1%}）"
             if avg and not _is_dash(dev) else fmt_num(avg, 2),
             _color_for(dev) if not _is_dash(dev) else None),
            ("获利比例", _with_level(f"{profit:.0%}" if not _is_dash(profit) else "—",
                                  chips.get("profit_level")), None),
            ("90%成本区间",
             f"{chips['cost_low']:.2f}~{chips['cost_high']:.2f}"
             if chips.get("cost_low") and chips.get("cost_high") else "—", None),
            ("筹码集中度", _with_level(f"{conc:.1%}" if not _is_dash(conc) else "—",
                                   chips.get("concentration_level")), None),
        ]

    risks = [r for r in (advice.get("risks") or [])
             if r and "不构成" not in r and "未考虑突发" not in r][:2]

    # 有 K 线就按「图 + 窄指标 + 结论」排；没有就把图压成一条提示，
    # 把高度让给指标和结论，避免卡片中间空一大块
    has_chart = hist is not None and len(hist) >= 5
    has_flow = has_chart and bool(flow_hist)

    # 自顶向下按「英寸」堆叠，而不是写死一堆百分比：面板多一块就只加画布高度，
    # 不用把其它面板的百分比统统重算一遍（重算最容易压字）。
    canvas_h = 13.7 if has_flow else 12.6
    def _rect(offset_in, height_in, x=0.055, w=0.90):
        top_in = canvas_h * 0.885          # 顶部色带上沿
        return [x, (top_in - offset_in - height_in) / canvas_h, w, height_in / canvas_h]

    cur = 0.30                             # 顶部留白
    chip_rect, flow_rect = None, None
    if has_chart and chips:
        kline_rect = _rect(cur, 2.95, 0.055, 0.775)
        chip_rect = [0.833, kline_rect[1], 0.117, kline_rect[3]]
        cur += 2.95 + 0.07
        vol_rect = _rect(cur, 0.85, 0.055, 0.775)
        cur += 0.85 + (0.34 if has_flow else 0.12)
    elif has_chart:
        kline_rect = _rect(cur, 3.00, 0.055, 0.90)
        cur += 3.00 + 0.07
        vol_rect = _rect(cur, 0.88, 0.055, 0.90)
        cur += 0.88 + (0.34 if has_flow else 0.12)
    else:
        kline_rect, vol_rect = _rect(0.10, 0.70), None
        cur = 0.10 + 0.70 + 0.12

    if has_flow:
        flow_rect = _rect(cur, 0.95)
        cur += 0.95 + 0.20
    metrics_rect = _rect(cur, 3.55)
    cur += 3.55 + 0.12
    advice_rect = _rect(cur, 2.30)

    try:
        fig = plt.figure(figsize=(9, canvas_h), dpi=120, facecolor=BG)
        _draw_header(fig, name=name, code=code, market=market, industry=industry,
                     price=price, pct=pct, cap=cap)
        price_ax = _draw_kline(fig, hist, name, kline_rect, vol_rect)
        if chip_rect and price_ax is not None:
            _draw_chips(fig, chips, chip_rect, price_ax)
        if flow_rect:
            _draw_flow(fig, flow_hist, flow_rect)
        _draw_metrics(fig, items, metrics_rect)
        _draw_advice(fig, advice, signals, risks, advice_rect)
        _draw_footer(fig, sources, generated_at)

        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out, dpi=120, facecolor=BG)
        plt.close(fig)
        print(f"[CARD] 已生成 {out} {out.stat().st_size // 1024}KB")
        return True
    except Exception as exc:  # noqa: BLE001  图片只是锦上添花，失败要退回文本
        print(f"::warning::生成卡片失败：{type(exc).__name__} {exc}")
        try:
            plt.close("all")
        except Exception:  # noqa: BLE001
            pass
        return False
