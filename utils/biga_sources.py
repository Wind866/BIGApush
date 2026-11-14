#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BIGApush 多数据源接入层（与 Wind866/limit-up-relay 同源容灾）。

底层能力来自同目录的 ``market_sources.py``（东财多域名 / 同花顺 / 新浪 /
腾讯，纯标准库、字段归一化、链式降级、健康日志），本文件把它映射到
BIGApush 的四条链路：

====================  ======================================================
链路                    多源顺序
====================  ======================================================
盘中选股强势股池         东财 clist 多域名 → 新浪全市场涨幅榜 → 同花顺涨停池
                       （备用池再依次用 东财 ulist.np / 腾讯行情 / 新浪资金流
                       补齐资金、量比、换手、流通市值）
交易日判断               东财指数时间戳（多域名）→ 腾讯指数行情时间戳
新闻要闻                 新浪滚动 2516 → 同花顺快讯 → 新浪 RSS → 新浪 2517
指数 / 板块行情           腾讯批量 → 新浪逐票 → 东财多域名；新浪行业 → 东财板块
====================  ======================================================

口径红线（与 limit-up-relay 一致）：

* 任何字段缺失一律保留 ``None``，绝不用 0 或猜测值冒充真实行情；
* 新浪资金流是「主动买卖单笔金额分级」口径，与东财不等价，会打标
  ``fund_comparable=False``，卡片必须提示分数不可与历史直接比较；
* 主力资金是按成交单笔金额推断的统计量，不是席位真实资金；
* 每次取数都记录来源、时点、成功/失败，可通过 ``check_sources.py`` 体检。

质量分级：``full`` 东财完整口径 → ``partial`` 备用源补齐部分字段
→ ``minimal`` 仅量价。评分口径由 ``source`` 决定：
``eastmoney`` 完整资金 / ``mixed`` 非东财资金 / 其余为纯量价。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any

try:  # 既支持 `python intraday_selection.py`，也支持 `python utils/biga_sources.py`
    from utils import market_sources as s
except ImportError:  # pragma: no cover - 直接运行本文件时补上项目根目录
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from utils import market_sources as s

BJ = s.CN_TZ
EM_HOSTS = tuple(s.EM_QUOTE_HOSTS) + ("http://push2.eastmoney.com",)
UT = s.UT
SINA_ROLL = "https://feed.mix.sina.com.cn/api/roll/get"
THS_NEWS = "https://news.10jqka.com.cn/tapp/news/push/stock/"
SINA_BOARD = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
EM_BOARD_FS = {"industry": "m:90+t:2", "concept": "m:90+t:3"}
INDEX_CODES = [
    ("sh000001", "1.000001", "s_sh000001", "000001", "上证指数"),
    ("sz399001", "0.399001", "s_sz399001", "399001", "深证成指"),
    ("sz399006", "0.399006", "s_sz399006", "399006", "创业板指"),
    ("sh000688", "1.000688", "s_sh000688", "000688", "科创50"),
]

# 东财 clist 一次给全字段：量价 + 量比 + 流通市值 + 主力/超大单 + 行业 + 行情时点
EM_FIELDS = "f12,f14,f2,f3,f6,f8,f10,f20,f21,f62,f184,f66,f69,f100,f124"

DEFAULTS: dict[str, Any] = {
    "timeout_seconds": 8,
    "max_retries": 2,
    "eastmoney": True,
    "eastmoney_clist": True,
    "ths": True,
    "sina": True,
    "tencent": True,
    "sina_flow_backfill": True,
    "news": True,
    "eastmoney_probe": True,
}
CFG: dict[str, Any] = dict(DEFAULTS)
CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "multi_source.json",
)


def configure(cfg: dict | None = None) -> dict:
    """读取 config/multi_source.json，允许用环境变量临时关闭某个源。

    注意：config/data_sources.json 是原仓库 data_fetcher.py 的供应商优先级配置，
    两者互不干扰，这里刻意使用独立文件。

    环境变量形如 ``BIGA_SOURCE_EASTMONEY=0``，便于本地强制模拟故障演练。
    """
    CFG.update(DEFAULTS)
    if cfg:
        CFG.update(cfg)
    elif os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                CFG.update(json.load(f) or {})
        except (OSError, ValueError) as exc:
            print(f"⚠️ 数据源配置读取失败，使用默认值：{exc}")
    for key in ("eastmoney", "eastmoney_clist", "ths", "sina", "tencent",
                "sina_flow_backfill", "news", "eastmoney_probe"):
        env = os.environ.get("BIGA_SOURCE_" + key.upper())
        if env is not None:
            CFG[key] = env.strip().lower() not in ("0", "false", "off", "no")
    s.configure({"data_sources": CFG})
    return CFG


@dataclass
class Snapshot:
    """盘中强势股快照：rows 统一为东财 f* 字段，供 build_pool 直接消费。"""
    rows: list[dict]
    quote_ts: int
    source: str                 # eastmoney / mixed / sina / tencent / ths
    quality: str                # full / partial / minimal
    fund_comparable: bool
    label: str
    notes: list[str] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)

    @property
    def has_fund(self) -> bool:
        return self.source in ("eastmoney", "mixed")


def _now_ts() -> int:
    return int(dt.datetime.now(BJ).timestamp())


# ---------------------------------------------------------------------------
# 东财通道探针（熔断）
# ---------------------------------------------------------------------------

# 东财 push2 对部分出口 IP 不是「超时」而是「直接 RST」：4 个域名 × 8 秒 × 2 次重试
# 全跑完要几十秒，而结论早在第一个请求就已经确定了。这里先用一个最小请求探一次，
# 不通就整轮跳过东财，把等待时间从几十秒压到 2 秒。
EM_PROBE_TTL = 300.0
_EM_PROBE: dict[str, Any] = {"ok": None, "at": 0.0, "detail": ""}


def em_probe(timeout: float = 2.5, force: bool = False) -> tuple[bool, str]:
    """探测东财行情通道是否可达，返回 (可用?, 说明)。

    结论在进程内缓存 5 分钟（``force=True`` 可强制重探）：盘中一次运行只需要
    一次真实探测，后续调用直接吃缓存，不会每页都付一次代价。
    """
    if not CFG.get("eastmoney_probe", True):
        return True, "探针已关闭，按配置直接尝试东财"
    now = time.time()
    if not force and _EM_PROBE["ok"] is not None and now - _EM_PROBE["at"] < EM_PROBE_TTL:
        return bool(_EM_PROBE["ok"]), str(_EM_PROBE["detail"])

    host = EM_HOSTS[0]
    url = f"{host}/api/qt/stock/get?secid=1.000001&ut={UT}&fields=f43"
    try:
        data = s.http_json(url, referer=s.EM_REFERER, timeout=timeout, retries=1)
        quote = (data.get("data") or {}).get("f43")
        ok = quote not in (None, "", "-", 0)
        detail = "指数报价正常" if ok else "返回空报价"
    except Exception as exc:  # noqa: BLE001 - 探针失败即视为不可用
        ok = False
        detail = f"{type(exc).__name__}: {str(exc)[:60]}"
    _EM_PROBE.update({"ok": ok, "at": now, "detail": detail})
    print(f"[数据源] 东财行情通道{'可达' if ok else '不可达'}（{detail}）")
    return ok, detail


def reset_probe() -> None:
    """清掉探针缓存（测试与自检用，避免上一轮结论污染下一轮）。"""
    _EM_PROBE.update({"ok": None, "at": 0.0, "detail": ""})


def em_limit_up_count(day: dt.date | None = None,
                      timeout: float = 4.0) -> tuple[int, str]:
    """东财涨停池家数，返回 (家数, 数据日期)。

    涨停池走 push2ex，与 push2 行情通道不是同一组域名：行情被拦时它常常还通，
    所以降级口径下的「涨停家数」用它做交叉校验，比单靠涨幅阈值判定可信。
    拿不到就抛错，由调用方决定是否写进容灾轨迹。
    """
    day = day or dt.datetime.now(BJ).date()
    pool = s.em_fetch_pool("ZT", day, timeout=timeout)
    items = pool.get("items") or []
    return int(pool.get("total") or len(items)), str(pool.get("qdate") or "")


def _row(code: str, name: str, ts: int) -> dict:
    """统一字段骨架，缺失字段一律 None，绝不猜测填 0。"""
    return {
        "f12": code, "f14": name, "f2": None, "f3": None, "f6": None,
        "f8": None, "f10": None, "f20": None, "f21": None,
        "f62": None, "f184": None, "f66": None, "f69": None,
        "f100": "其他", "f124": ts,
    }


def _f(v: Any) -> float | None:
    return s.num(v)


# ---------------------------------------------------------------------------
# 供应商：强势股候选池
# ---------------------------------------------------------------------------

def em_strong_stocks(scan_floor: float = 2.0, max_pages: int = 20) -> tuple[list[dict], int]:
    """东财 clist 按涨幅降序分页；同一供应商多域名轮换，任一域名可用即返回完整口径。"""
    last: Exception | None = None
    for host in EM_HOSTS:
        rows: list[dict] = []
        ts = 0
        try:
            for page in range(1, max_pages + 1):
                url = (f"{host}/api/qt/clist/get?pn={page}&pz=100&po=1&np=1"
                       f"&fltt=2&invt=2&ut={UT}&fid=f3&fs={s.EM_CLIST_FS}"
                       f"&fields={EM_FIELDS}")
                data = s.http_json(url, referer=s.EM_REFERER, timeout=8, retries=1)
                batch = ((data.get("data") or {}).get("diff") or [])
                if isinstance(batch, dict):
                    batch = list(batch.values())
                if not batch:
                    break
                rows.extend(batch)
                for r in batch:
                    ts = max(ts, int(_f(r.get("f124")) or 0))
                tail = _f(batch[-1].get("f3"))
                if len(batch) < 100 or (tail is not None and tail < scan_floor):
                    break
                time.sleep(0.12)
            if not rows:
                raise s.SourceFailure(f"{host} 返回空榜单")
            return rows, ts or _now_ts()
        except Exception as exc:  # noqa: BLE001 - 单域名失败即换下一个
            last = exc
    raise s.SourceFailure(
        f"东财榜单多域名均失败: {type(last).__name__} {str(last)[:100]}")


def sina_strong_stocks(scan_floor: float = 2.0, max_pages: int = 20) -> tuple[list[dict], int]:
    """新浪全市场涨幅榜（量价口径，不含资金/量比/行业）。"""
    rows: list[dict] = []
    ts = _now_ts()
    for page in range(1, max_pages + 1):
        url = (f"{s.SINA_RANK}?page={page}&num=100&sort=changepercent"
               f"&asc=0&node=hs_a")
        text = s.http_text(url, referer=s.SINA_REFERER, timeout=9, retries=2)
        try:
            batch = json.loads(text)
        except json.JSONDecodeError as exc:
            raise s.SourceFailure(f"新浪榜单非 JSON: {text[:60]}") from exc
        if not isinstance(batch, list) or not batch:
            break
        for d in batch:
            code = str(d.get("code") or "").strip()
            if not re.fullmatch(r"\d{6}", code):
                continue
            r = _row(code, str(d.get("name") or "").strip(), ts)
            mktcap, nmc = _f(d.get("mktcap")), _f(d.get("nmc"))
            r.update({
                "f2": _f(d.get("trade")), "f3": _f(d.get("changepercent")),
                "f6": _f(d.get("amount")), "f8": _f(d.get("turnoverratio")),
                "f20": mktcap * 1e4 if mktcap is not None else None,
                "f21": nmc * 1e4 if nmc is not None else None,
                "f100": "其他",
            })
            rows.append(r)
        tail = _f(batch[-1].get("changepercent")) or 0
        if tail < scan_floor or len(batch) < 100:
            break
        time.sleep(0.15)
    if not rows:
        raise s.SourceFailure("新浪全市场涨幅榜为空")
    return rows, ts


def ths_strong_stocks(day: dt.date | None = None) -> tuple[list[dict], int]:
    """同花顺涨停池：极端情况下的最小兜底（只有涨停股，覆盖窄）。"""
    day = day or dt.datetime.now(BJ).date()
    pool = s.ths_fetch_limit_up(day)
    ts = _now_ts()
    rows: list[dict] = []
    for it in pool.get("items") or []:
        r = _row(str(it.get("code") or ""), str(it.get("name") or ""), ts)
        r.update({
            "f2": it.get("price"), "f3": it.get("pct"),
            "f6": it.get("amount"), "f8": it.get("turnover"),
            "f21": it.get("float_mv"), "f100": it.get("industry") or "其他",
        })
        rows.append(r)
    if not rows:
        raise s.SourceFailure("同花顺涨停池为空")
    return rows, ts


# ---------------------------------------------------------------------------
# 备用池字段补齐
# ---------------------------------------------------------------------------

def _codes(rows: list[dict], limit: int = 200) -> list[str]:
    out: list[str] = []
    for r in rows:
        code = str(r.get("f12") or "")
        if re.fullmatch(r"\d{6}", code) and code not in out:
            out.append(code)
        if len(out) >= limit:
            break
    return out


def _quote_to_row(q: dict, ts: int) -> dict:
    r = _row(q["code"], q.get("name") or "", ts)
    r.update({
        "f2": q.get("price"), "f3": q.get("pct"), "f6": q.get("amount"),
        "f8": q.get("turnover"), "f10": q.get("vol_ratio"),
        "f20": q.get("total_mv"), "f21": q.get("float_mv"),
        "f62": q.get("main_net"), "f184": q.get("main_pct"),
        "f66": q.get("super_net"), "f69": q.get("super_pct"),
        "f100": q.get("industry") or "其他",
    })
    return r


def em_upgrade(rows: list[dict]) -> tuple[list[dict], float]:
    """备用池拿到代码后，尝试用东财 ulist.np（多域名）换回完整资金口径。

    返回 (合并后的 rows, 覆盖率)。覆盖率不足 50% 时由调用方放弃升级。
    """
    codes = _codes(rows)
    if not codes:
        return rows, 0.0
    quotes: dict[str, dict] = {}
    for i in range(0, len(codes), 45):
        chunk = codes[i:i + 45]
        try:
            quotes.update(s.em_fetch_quotes([s.secid(c) for c in chunk], True))
        except Exception:  # noqa: BLE001 - 单批失败不影响其它批
            pass
        time.sleep(0.1)
    if not quotes:
        return rows, 0.0
    ts = _now_ts()
    merged: list[dict] = []
    hit = 0
    for r in rows:
        q = quotes.get(str(r.get("f12") or ""))
        if q and q.get("main_net") is not None:
            merged.append(_quote_to_row(q, int(_f(r.get("f124")) or ts)))
            hit += 1
        else:
            merged.append(r)
    return merged, hit / max(1, len(codes))


def tencent_patch(rows: list[dict]) -> int:
    """腾讯行情补量比 / 换手 / 流通市值（无资金拆解）。"""
    codes = _codes(rows)
    if not codes:
        return 0
    quotes: dict[str, dict] = {}
    for i in range(0, len(codes), 50):
        try:
            quotes.update(s.tencent_fetch_quotes(codes[i:i + 50]))
        except Exception:  # noqa: BLE001
            pass
    mapping = (("f2", "price"), ("f3", "pct"), ("f6", "amount"),
               ("f8", "turnover"), ("f10", "vol_ratio"),
               ("f20", "total_mv"), ("f21", "float_mv"))
    n = 0
    for r in rows:
        q = quotes.get(str(r.get("f12") or ""))
        if not q:
            continue
        changed = False
        for field_, key in mapping:
            if r.get(field_) is None and q.get(key) is not None:
                r[field_] = q[key]
                changed = True
        n += int(changed)
    return n


def sina_flow_patch(rows: list[dict]) -> int:
    """新浪资金流补当日主力/超大单（口径不同，调用方必须标注不可比）。"""
    if not CFG.get("sina_flow_backfill", True):
        return 0
    codes = [str(r.get("f12") or "") for r in rows
             if r.get("f62") is None and re.fullmatch(r"\d{6}", str(r.get("f12") or ""))]
    codes = list(dict.fromkeys(codes))[:120]
    if not codes:
        return 0
    patch: dict[str, dict] = {}
    for i in range(0, len(codes), 30):
        try:
            patch.update(s.sina_fetch_flow(codes[i:i + 30], batch=30))
        except Exception:  # noqa: BLE001
            pass
    n = 0
    for r in rows:
        p = patch.get(str(r.get("f12") or ""))
        if not p or r.get("f62") is not None:
            continue
        r.update({
            "f62": p.get("main_net"), "f184": p.get("main_pct"),
            "f66": p.get("super_net"), "f69": p.get("super_pct"),
        })
        n += 1
    return n


def fetch_snapshot(scan_floor: float = 2.0, max_pages: int = 20,
                   cfg: dict | None = None) -> Snapshot:
    """盘中强势股统一入口：候选池降级 → 二级补齐 → 质量分级。

    调用方只需关心 ``snapshot.source`` 与 ``snapshot.has_fund``；
    所有失败尝试都记录在 ``snapshot.attempts``，可写日志或 Job Summary。
    """
    configure(cfg)
    s.HEALTH.reset()
    day = dt.datetime.now(BJ).date()
    notes: list[str] = []
    providers: list[s.Provider] = []

    # 先探一次东财：不通就别把「不可用」放大成几十秒的域名轮询
    em_ok, em_detail = em_probe() if CFG.get("eastmoney", True) else (False, "东财已按配置停用")
    if CFG.get("eastmoney", True):
        if em_ok:
            providers.append(s.Provider(
                "eastmoney", "东方财富实时榜单（含主力资金）", "full",
                "push2.eastmoney.com · clist 多域名",
                lambda: em_strong_stocks(scan_floor, max_pages)))
        else:
            notes.append(f"东财通道熔断，本轮跳过该源（{em_detail}）；改用腾讯/新浪补字段")
    if CFG.get("sina", True):
        providers.append(s.Provider(
            "sina", "新浪财经全市场涨幅榜（量价）", "minimal",
            "vip.stock.finance.sina.com.cn · Market_Center",
            lambda: sina_strong_stocks(scan_floor, max_pages)))
    if CFG.get("ths", True):
        providers.append(s.Provider(
            "ths", "同花顺涨停池（最小兜底）", "minimal",
            "data.10jqka.com.cn · limit_up_pool",
            lambda: ths_strong_stocks(day)))
    if not providers:
        raise s.AllSourcesFailed("snapshot", [])

    out = s.chain("snapshot", providers,
                  lambda v: f"{len(v[0])} 只强势股 / 时点 {v[1]}")
    rows, quote_ts = out.value
    source, quality = out.provider, out.quality
    notes.append(f"候选池来源：{out.label}")
    comparable = source == "eastmoney"

    if quality != "full":
        # 1) 东财通的时候才值得用 ulist.np 换回完整口径（榜单挂了不等于行情挂了）
        if em_ok:
            try:
                upgraded, coverage = em_upgrade(rows)
                if coverage >= 0.5:
                    rows, source, quality, comparable = upgraded, "eastmoney", "full", True
                    notes.append(f"东财 ulist.np 补齐 {coverage:.0%} 只完整字段（含资金），恢复同口径评分")
                else:
                    notes.append(f"东财行情通道覆盖率仅 {coverage:.0%}，保持备用口径")
            except Exception as exc:  # noqa: BLE001
                notes.append(f"东财行情通道不可用（{type(exc).__name__}），改用跨供应商补齐")
        if CFG.get("tencent", True):
            n = tencent_patch(rows)
            if n:
                notes.append(f"腾讯行情补齐 {n} 只量比 / 换手 / 流通市值")
        n = sina_flow_patch(rows)
        if n:
            notes.append(f"新浪资金流补齐 {n} 只当日资金拆解（口径与东财不等价）")

    has_fund = any(r.get("f62") is not None for r in rows)
    if has_fund and source != "eastmoney":
        source, quality, comparable = "mixed", "partial", False
        notes.append("资金来自备用口径，本期分数不可与历史东财口径直接比较")
    elif not has_fund and source == "eastmoney":
        source, quality = "sina", "minimal"

    return Snapshot(
        rows=rows, quote_ts=quote_ts or _now_ts(), source=source,
        quality=quality, fund_comparable=comparable, label=out.label,
        notes=notes, attempts=list(s.HEALTH.attempts),
    )


# ---------------------------------------------------------------------------
# 交易日判断：指数时间戳
# ---------------------------------------------------------------------------

def fetch_index_timestamp() -> tuple[int, str]:
    """返回 (行情时间戳, 来源说明)；全部失败时抛错，由调用方保守放行。"""
    last: Exception | None = None
    # 东财被拦时不该逐个域名硬撞 4 次：探针判死就直接走腾讯
    if em_probe(timeout=2.0)[0]:
        for host in EM_HOSTS:
            try:
                url = f"{host}/api/qt/stock/get?secid=1.000001&ut={UT}&fields=f43,f86"
                data = s.http_json(url, referer=s.EM_REFERER, timeout=6, retries=1)
                ts = int((data.get("data") or {}).get("f86") or 0)
                if ts > 1_000_000_000:
                    return ts, f"东财指数时间戳（{host.split('//')[-1]}）"
            except Exception as exc:  # noqa: BLE001
                last = exc
    try:
        text = s.http_text("https://qt.gtimg.cn/q=sh000001",
                           referer=s.TENCENT_REFERER, encoding="gbk",
                           timeout=6, retries=1)
        p = text.split("~")
        stamp = (p[30] if len(p) > 30 else "").strip()
        if re.fullmatch(r"\d{14}", stamp):
            d = dt.datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=BJ)
            return int(d.timestamp()), "腾讯指数行情时间戳"
    except Exception as exc:  # noqa: BLE001
        last = exc
    raise s.SourceFailure(
        f"指数行情全部数据源不可用: {type(last).__name__} {str(last)[:90]}")


# ---------------------------------------------------------------------------
# 新闻 / 指数 / 板块（早间与晚间新闻共用）
# ---------------------------------------------------------------------------

def _news_lines(items: list[tuple[str, str]], count: int) -> list[str]:
    lines: list[str] = []
    for title, intro in items[:count]:
        title = (title or "").strip()
        if not title:
            continue
        lines.append(f"  • {title}")
        intro = re.sub(r"\s+", " ", (intro or "").strip())[:110]
        if intro:
            lines.append(f"    {intro}")
    if not lines:
        raise s.SourceFailure("新闻源返回空列表")
    return lines


def _news_sina_roll(lid: int, count: int) -> list[str]:
    url = f"{SINA_ROLL}?pageid=153&lid={lid}&k=&num={count + 5}&page=1"
    text = s.http_text(url, referer=s.SINA_REFERER, timeout=8, retries=1)
    data = json.loads(text)
    if (data.get("result") or {}).get("status", {}).get("code") not in (0, None):
        raise s.SourceFailure(f"新浪滚动 lid={lid} 返回异常状态")
    items = (data.get("result") or {}).get("data") or []
    return _news_lines([(n.get("title", ""), n.get("intro", "")) for n in items], count)


def _news_ths(count: int) -> list[str]:
    url = f"{THS_NEWS}?page=1&tag=&track=website&pagesize={count + 5}"
    text = s.http_text(url, referer="https://news.10jqka.com.cn/",
                       timeout=8, retries=1)
    data = json.loads(text)
    items = (data.get("data") or {}).get("list") or []
    return _news_lines([(n.get("title", ""), n.get("digest", "")) for n in items], count)


def _news_ths_page2(count: int) -> list[str]:
    url = f"{THS_NEWS}?page=2&tag=&track=website&pagesize={count + 5}"
    text = s.http_text(url, referer="https://news.10jqka.com.cn/",
                       timeout=8, retries=1)
    items = (json.loads(text).get("data") or {}).get("list") or []
    return _news_lines([(n.get("title", ""), n.get("digest", "")) for n in items], count)


def fetch_news(count: int = 10) -> tuple[list[str], str]:
    """新闻五级降级：新浪 2516 → 同花顺快讯 → 新浪 2515 → 新浪 2517 → 同花顺次页。

    原先的 sina RSS 已下线（返回空 channel），已替换为实测可用的滚动栏目。
    """
    providers = [
        s.Provider("sina", "新浪财经滚动要闻", "full", "feed.mix.sina.com.cn · lid=2516",
                   lambda: _news_sina_roll(2516, count)),
        s.Provider("ths", "同花顺 7×24 快讯", "full", "news.10jqka.com.cn · page=1",
                   lambda: _news_ths(count)),
        s.Provider("sina", "新浪财经备用栏目 A", "partial",
                   "feed.mix.sina.com.cn · lid=2515",
                   lambda: _news_sina_roll(2515, count)),
        s.Provider("sina", "新浪财经备用栏目 B", "partial",
                   "feed.mix.sina.com.cn · lid=2517",
                   lambda: _news_sina_roll(2517, count)),
        s.Provider("ths", "同花顺快讯次页", "partial", "news.10jqka.com.cn · page=2",
                   lambda: _news_ths_page2(count)),
    ]
    out = s.chain("news", providers, lambda v: f"{len(v)} 行")
    return out.value, out.label


def _index_line(name: str, price: Any, pct: Any, amount_yi: Any) -> str:
    try:
        p = float(pct)
    except (TypeError, ValueError):
        p = 0.0
    emoji = "🔴" if p > 0 else "🟢" if p < 0 else "⚪"
    vol = f" {float(amount_yi):.0f}亿" if amount_yi not in (None, "") else ""
    return f"  {emoji} {name}: {price} ({pct}%){vol}"


def _index_tencent() -> list[str]:
    codes = ",".join(x[0] for x in INDEX_CODES)
    text = s.http_text(f"{s.TENCENT_HQ}{codes}", referer=s.TENCENT_REFERER,
                       encoding="gbk", timeout=8, retries=2)
    by_code = {x[0][2:]: x[4] for x in INDEX_CODES}
    lines: list[str] = []
    for seg in text.split(";"):
        if "~" not in seg:
            continue
        p = seg.split("~")
        if len(p) < 46:
            continue
        name = by_code.get(p[2], p[1])
        amount_wan = _f(p[37])
        lines.append(_index_line(name, p[3], p[32],
                                 amount_wan / 1e4 if amount_wan is not None else None))
    if len(lines) < 3:
        raise s.SourceFailure("腾讯指数行情不足")
    return lines


def _index_sina() -> list[str]:
    codes = ",".join(x[2] for x in INDEX_CODES)
    text = s.http_text(f"{s.SINA_HQ}{codes}", referer=s.SINA_REFERER,
                       encoding="gbk", timeout=8, retries=2)
    by_code = {x[2].replace("s_", ""): x[4] for x in INDEX_CODES}
    lines: list[str] = []
    for m in re.finditer(r'hq_str_(s_\w+)="([^"]+)"', text):
        parts = m.group(2).split(",")
        if len(parts) < 6:
            continue
        name = by_code.get(m.group(1), parts[0])
        amount_wan = _f(parts[5])
        lines.append(_index_line(name, parts[1], parts[3],
                                 amount_wan / 1e4 if amount_wan is not None else None))
    if len(lines) < 3:
        raise s.SourceFailure("新浪指数行情不足")
    return lines


def _index_em() -> list[str]:
    secids = ",".join(x[1] for x in INDEX_CODES)
    by_code = {x[1].split(".")[-1]: x[4] for x in INDEX_CODES}
    last: Exception | None = None
    for host in EM_HOSTS:
        try:
            url = (f"{host}/api/qt/ulist.np/get?fltt=2&invt=2&ut={UT}"
                   f"&secids={secids}&fields=f12,f14,f2,f3,f6")
            data = s.http_json(url, referer=s.EM_REFERER, timeout=7, retries=1)
            rows = ((data.get("data") or {}).get("diff") or [])
            if isinstance(rows, dict):
                rows = list(rows.values())
            lines = []
            for r in rows:
                code = str(r.get("f12") or "")
                amount = _f(r.get("f6"))
                lines.append(_index_line(by_code.get(code, r.get("f14") or code),
                                         r.get("f2"), r.get("f3"),
                                         amount / 1e8 if amount is not None else None))
            if len(lines) < 3:
                raise s.SourceFailure("东财指数行情不足")
            return lines
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise s.SourceFailure(f"东财指数多域名均失败: {type(last).__name__}")


def fetch_index_lines() -> tuple[list[str], str]:
    """早/晚新闻的大盘指数行：腾讯批量 → 新浪 → 东财多域名。"""
    out = s.chain("index", [
        s.Provider("tencent", "腾讯证券批量指数", "full", "qt.gtimg.cn", _index_tencent),
        s.Provider("sina", "新浪财经指数", "full", "hq.sinajs.cn", _index_sina),
        s.Provider("eastmoney", "东方财富指数（多域名）", "full",
                   "push2.eastmoney.com · ulist.np", _index_em),
    ], lambda v: f"{len(v)} 个指数")
    return out.value, out.label


def sina_board(kind: str = "industry") -> list[dict]:
    """新浪行业板块榜（纯标准库版，供健康自检与降级探测使用）。"""
    url = SINA_BOARD if kind == "industry" else \
        "https://vip.stock.finance.sina.com.cn/q/view/newSinaGN.php"
    text = s.http_text(url, referer=s.SINA_REFERER, encoding="gbk",
                       timeout=8, retries=2)
    m = re.search(r"=\s*(\{.*\})", text, re.DOTALL)
    if not m:
        raise s.SourceFailure("新浪板块返回格式变化")
    data = json.loads(m.group(1))
    out: list[dict] = []
    for v in data.values():
        parts = str(v).split(",")
        if len(parts) > 5:
            out.append({"name": parts[1], "pct": _f(parts[4]), "main_net": None})
    out.sort(key=lambda x: x["pct"] or 0, reverse=True)
    if not out:
        raise s.SourceFailure("新浪板块为空")
    return out


def fetch_board_em(kind: str, sort_field: str = "f3",
                   limit: int = 100) -> list[dict]:
    """东财板块榜（行业 / 概念），多域名轮换；新浪板块接口失效时启用。

    返回 ``[{"name", "pct", "main_net"}]``，按 sort_field 降序。
    """
    fs = EM_BOARD_FS.get(kind, EM_BOARD_FS["industry"])
    last: Exception | None = None
    for host in EM_HOSTS:
        try:
            url = (f"{host}/api/qt/clist/get?pn=1&pz={limit}&po=1&np=1"
                   f"&fltt=2&invt=2&ut={UT}&fid={sort_field}&fs={fs}"
                   f"&fields=f12,f14,f3,f62")
            data = s.http_json(url, referer=s.EM_REFERER, timeout=8, retries=1)
            rows = ((data.get("data") or {}).get("diff") or [])
            if isinstance(rows, dict):
                rows = list(rows.values())
            out = [{"name": str(r.get("f14") or ""),
                    "pct": _f(r.get("f3")),
                    "main_net": _f(r.get("f62"))} for r in rows
                   if str(r.get("f12") or "")]
            if not out:
                raise s.SourceFailure("东财板块返回空")
            return out
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise s.SourceFailure(f"东财板块多域名均失败: {type(last).__name__}")


# ---------------------------------------------------------------------------
# 健康自检探针（供 check_sources.py 使用）
# ---------------------------------------------------------------------------

def probes() -> list[tuple[str, str, str, str, Any]]:
    """返回 (分组, 供应商, 中文名, 接口, 调用)。只读探测，不产生任何信号。"""
    day = dt.datetime.now(BJ).date()
    codes = ["300528", "600519"]
    secids = ["0.300528", "1.600519"]
    return [
        ("snapshot", "eastmoney", "东财实时强势榜（主，多域名）", "push2 · clist",
         lambda: f"{len(em_strong_stocks(8.0, 1)[0])} 只"),
        ("snapshot", "eastmoney", "东财批量行情＋资金（补齐全口径）", "push2 · ulist.np",
         lambda: f"{len(s.em_fetch_quotes(secids))} 只"),
        ("snapshot", "eastmoney_clist", "东财榜单通道（同口径备 1）", "push2 · clist",
         lambda: f"{len(s.em_fetch_quotes_clist(secids))} 只"),
        ("snapshot", "eastmoney", "东财涨停池（交叉校验）", "push2ex · ZTPool",
         lambda: f"{len(s.em_fetch_pool('ZT', day)['items'])} 只"),
        ("snapshot", "ths", "同花顺涨停池（备 2）", "data.10jqka.com.cn",
         lambda: f"{len(s.ths_fetch_limit_up(day)['items'])} 只"),
        ("snapshot", "sina", "新浪全市场涨幅榜（备 3）", "Market_Center.getHQNodeData",
         lambda: f"{len(sina_strong_stocks(8.0, 1)[0])} 只"),
        ("snapshot", "sina", "新浪逐票行情（备 4）", "hq.sinajs.cn",
         lambda: f"{len(s.sina_fetch_quotes(codes))} 只"),
        ("snapshot", "tencent", "腾讯证券行情（量比/换手补齐）", "qt.gtimg.cn",
         lambda: f"{len(s.tencent_fetch_quotes(codes))} 只"),
        ("flow", "sina_flow", "新浪资金流（口径不同）", "MoneyFlow.ssi_ssfx_flzjtj",
         lambda: f"{len(s.sina_fetch_flow(codes, batch=10))} 只"),
        ("calendar", "eastmoney", "东财指数时间戳（交易日判断）", "push2 · stock/get",
         lambda: f"{fetch_index_timestamp()[1]}"),
        ("calendar", "tencent", "腾讯指数时间戳（备）", "qt.gtimg.cn",
         lambda: f"{len(_index_tencent())} 个指数"),
        ("news", "sina", "新浪财经滚动要闻（主）", "feed.mix.sina.com.cn · 2516",
         lambda: f"{len(_news_sina_roll(2516, 5))} 行"),
        ("news", "ths", "同花顺 7×24 快讯（备 1）", "news.10jqka.com.cn · page=1",
         lambda: f"{len(_news_ths(5))} 行"),
        ("news", "sina", "新浪财经备用栏目 A（备 2）", "feed.mix.sina.com.cn · 2515",
         lambda: f"{len(_news_sina_roll(2515, 5))} 行"),
        ("news", "sina", "新浪财经备用栏目 B（备 3）", "feed.mix.sina.com.cn · 2517",
         lambda: f"{len(_news_sina_roll(2517, 5))} 行"),
        ("news", "tencent", "腾讯批量指数（新闻大盘）", "qt.gtimg.cn",
         lambda: f"{len(_index_tencent())} 个指数"),
        ("board", "sina", "新浪行业板块（主）", "newSinaHy.php",
         lambda: f"{len(sina_board('industry'))} 个"),
        ("board", "eastmoney", "东财行业板块（新浪失效时启用）", "push2 · clist 板块",
         lambda: f"{len(fetch_board_em('industry', limit=10))} 个"),
    ]


if __name__ == "__main__":
    configure()
    snap = fetch_snapshot(scan_floor=8.0, max_pages=2)
    print(f"来源={snap.source} 质量={snap.quality} 资金可比={snap.fund_comparable} "
          f"行数={len(snap.rows)} 时点={snap.quote_ts}")
    for n in snap.notes:
        print("  -", n)
