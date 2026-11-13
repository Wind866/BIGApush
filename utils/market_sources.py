"""多数据源适配与容灾层（纯标准库，GitHub Actions 无需 pip install）。

解决的问题
----------
原实现所有数据都来自东方财富单一家供应商，一旦 ``push2ex`` / ``ulist``
限流、改字段或下线，整条流水线只能回退到历史 demo 快照。本模块把取数拆成
四层独立供应商，按优先级自动切换，并对所有来源做字段归一化：

====================  ==========================================================
供应商                 覆盖能力
====================  ==========================================================
东方财富（主）          涨停/炸板/跌停池、行情、四类单资金拆解、5/10日资金、逐日资金流
同花顺（备 1）          涨停池（含封板时间/封单额/炸板次数/连板高度），用于交叉校验
新浪财经（备 2）        全市场涨跌幅榜 → 重建涨停/炸板/跌停池；逐票行情
腾讯证券（备 3）        逐票行情（含量比、换手、流通市值、涨跌停价）
====================  ==========================================================

冗余分两级：
1. **同供应商多域名**：东财 push2 系列有 ``82.`` / ``push2delay`` 等镜像，
   push2his 有 ``82.`` 镜像，push2ex 有 http 降级；
2. **跨供应商**：同一供应商所有域名都被限流时（实测常见），切到同花顺 /
   新浪 / 腾讯，保证 13:00 与 15:30 两次运行仍能出数据。

质量分级（写入 snapshot.source_health，前端与飞书卡片会显示）：
* ``full``    —— 东财主源，字段齐全，可与历史分数直接比较；
* ``partial`` —— 至少一个备用源，部分字段（如资金拆解、行业）缺失；
* ``minimal`` —— 仅量价口径，缺封板质量与全部资金拆解，评分可比性下降。

口径红线
--------
* 任何字段缺失一律返回 ``None``，**绝不用 0 或猜测值冒充真实行情**；
  评分层对 None 走中性 50 分（见 strategy._piecewise）。
* 同花顺备用源未提供成交额时，按「流通市值 × 换手率」推导（换手率定义即
  成交量/流通股本），并在 ``source_health.notes`` 中显式标注为推导值。
* 主力资金为按成交单笔金额推断的统计量，非交易所席位真实资金。
* 每个返回值都带来源标识与行情时点，可追溯到具体接口。
"""

from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

CN_TZ = dt.timezone(dt.timedelta(hours=8))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
EM_REFERER = "https://quote.eastmoney.com/"
THS_REFERER = "https://data.10jqka.com.cn/"
SINA_REFERER = "https://finance.sina.com.cn/"
TENCENT_REFERER = "https://gu.qq.com/"

UT = "7eea3edcaed734bea9cbfc24409ed989"
UT_HIS = "b2884a393a59ad64002292a3e90d46a5"

# 同一供应商的多个接入点（按优先级）
EM_POOL_HOSTS = ("https://push2ex.eastmoney.com", "http://push2ex.eastmoney.com")
EM_QUOTE_HOSTS = ("https://push2.eastmoney.com",
                  "https://82.push2.eastmoney.com",
                  "https://push2delay.eastmoney.com")
EM_HISTORY_HOSTS = ("https://push2his.eastmoney.com",
                    "https://82.push2his.eastmoney.com")

THS_API = "https://data.10jqka.com.cn/dataapi"
SINA_HQ = "https://hq.sinajs.cn/list="
SINA_RANK = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/"
             "json_v2.php/Market_Center.getHQNodeData")
SINA_FLOW = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/"
             "json_v2.php/MoneyFlow.ssi_ssfx_flzjtj")
TENCENT_HQ = "https://qt.gtimg.cn/q="
EM_CLIST_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"  # 沪深主板+创业板+科创板

THS_FIELDS = ("199112,10,9001,330323,330324,330325,9002,330329,133971,"
              "133970,1968584,3475914,9003,9004")

# 由 config.json → data_sources 覆盖；调小可让整条链更快失败并降级
DEFAULT_TIMEOUT = 8.0
DEFAULT_RETRIES = 2


def configure(cfg: dict) -> None:
    """应用 data_sources 中的超时/重试上限（每次 collect 前调用）。"""
    global DEFAULT_TIMEOUT, DEFAULT_RETRIES
    sc = cfg.get("data_sources", {})
    try:
        DEFAULT_TIMEOUT = max(1.0, float(sc.get("timeout_seconds", 8)))
    except (TypeError, ValueError):
        DEFAULT_TIMEOUT = 8.0
    try:
        DEFAULT_RETRIES = max(1, int(sc.get("max_retries", 2)))
    except (TypeError, ValueError):
        DEFAULT_RETRIES = 2

# 实时资金流字段：当日四类单 + 5 日四类单 + 10 日主力，另补量价与行情时间戳
FLOW_FIELDS = (
    "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,"
    "f164,f165,f166,f167,f168,f169,f170,f171,f172,f173,f174,f175,"
    "f6,f8,f10,f20,f21,f124"
)
QUOTE_FIELDS = "f12,f14,f2,f3,f6,f8,f10,f20,f21,f124"

PROVIDER_LABELS = {
    "eastmoney": "东方财富",
    "ths": "同花顺",
    "sina": "新浪财经",
    "tencent": "腾讯证券",
    "eastmoney_clist": "东方财富(榜单通道)",
    "sina_flow": "新浪资金流(口径不同)",
    "cache": "本次运行缓存",
}

# 新浪榜单在同一次 collect 内复用，避免重建三种池时重复分页请求
_SINA_RANK_CACHE: dict[tuple[str, bool], tuple[float, list[dict]]] = {}


class SourceFailure(RuntimeError):
    """单个数据源不可用（可安全降级到下一源）。"""


class AllSourcesFailed(SourceFailure):
    def __init__(self, group: str, attempts: list[dict]):
        self.group = group
        self.attempts = attempts
        tried = "、".join(a["label"] for a in attempts) or "无"
        super().__init__(f"{group} 全部数据源失效（已尝试：{tried}）")


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def now_cn() -> dt.datetime:
    return dt.datetime.now(CN_TZ)


def date_compact(day: dt.date | None = None) -> str:
    return (day or now_cn().date()).strftime("%Y%m%d")


def date_iso(day: dt.date | None = None) -> str:
    return (day or now_cn().date()).strftime("%Y-%m-%d")


def num(value: Any) -> float | None:
    """字段转 float；'-' / None / 空串 / 非数字一律 None，绝不填 0 冒充行情。"""
    if value is None or value in ("-", "", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def secid(code: str, market: int | None = None) -> str:
    if market is None:
        market = 1 if code.startswith(("60", "68", "9", "5")) else 0
    return f"{int(market)}.{code}"


def compact_to_iso(value: Any) -> str:
    text = str(value)
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


def _headers(referer: str) -> dict[str, str]:
    return {"User-Agent": UA, "Referer": referer, "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9"}


def http_text(url: str, *, referer: str = EM_REFERER, encoding: str = "utf-8",
              timeout: float = 8.0, retries: int = 2) -> str:
    """带短重试的 GET。同一 host 重试后才交给上层换域名/换供应商。

    各接口传入的 timeout/retries 是该接口的偏好上限，最终不超过
    config.json → data_sources 配置的全局上限（便于统一加速失败降级）。
    """
    timeout = min(float(timeout), DEFAULT_TIMEOUT)
    retries = max(1, min(int(retries), DEFAULT_RETRIES))
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=_headers(referer))
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            text = raw.decode(encoding, "ignore")
            if not text.strip():
                raise SourceFailure("空响应")
            return text
        except Exception as exc:  # noqa: BLE001 - 网络异常统一重试/降级
            last = exc
            if attempt < retries - 1:
                time.sleep(0.3 * (attempt + 1))
    raise SourceFailure(f"{type(last).__name__}: {str(last)[:110]}")


def http_json(url: str, **kw) -> dict:
    text = http_text(url, **kw)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SourceFailure(f"非 JSON 响应: {text[:50]}") from exc
    if not isinstance(data, dict):
        raise SourceFailure("JSON 顶层结构不是对象")
    return data


def _at(parts: list[str], idx: int) -> float | None:
    return num(parts[idx]) if idx < len(parts) else None


def limit_pct(code: str, name: str) -> float:
    """A 股涨跌停幅度（用于从全市场行情重建池）。"""
    upper = (name or "").upper()
    if "ST" in upper:
        return 5.0
    if code.startswith(("30", "68")):
        return 20.0
    if code.startswith(("8", "4", "92")):
        return 30.0
    return 10.0


def _limit_price(prev_close: float, pct: float) -> float:
    return round(prev_close * (1 + pct / 100) + 1e-8, 2)


def _market_of(code: str) -> int:
    return 1 if code.startswith(("60", "68", "9", "5")) else 0


def _tx_symbol(code: str) -> str:
    if code.startswith(("8", "4", "92")):
        return "bj" + code
    return ("sh" if _market_of(code) == 1 else "sz") + code


# ---------------------------------------------------------------------------
# 容灾调度与健康日志
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Provider:
    key: str            # eastmoney / ths / sina / tencent
    label: str          # 中文名，用于日志与看板
    quality: str        # 该源能提供的最高数据质量
    endpoint: str       # 具体接口/域名描述
    call: Callable[[], Any]


@dataclass
class Outcome:
    value: Any
    provider: str
    label: str
    quality: str
    endpoint: str
    attempts: list[dict]


class HealthLog:
    """一次 collect 内所有取数尝试的记录，最终写入 snapshot.source_health。"""

    def __init__(self) -> None:
        self.attempts: list[dict] = []

    def reset(self) -> None:
        self.attempts = []
        _SINA_RANK_CACHE.clear()

    def add(self, group: str, provider: str, label: str, endpoint: str,
            ok: bool, ms: int, detail: str) -> None:
        self.attempts.append({
            "group": group, "provider": provider, "label": label,
            "endpoint": endpoint, "ok": ok, "ms": ms, "detail": str(detail)[:180],
        })

    def to_dict(self, groups: dict[str, dict], quality: str,
                notes: list[str], cross_check: dict) -> dict:
        failed = [a for a in self.attempts if not a["ok"]]
        return {
            "checked_at": now_cn().strftime("%Y-%m-%d %H:%M:%S %z"),
            "quality": quality,
            "degraded": quality != "full",
            "groups": groups,
            "cross_check": cross_check,
            "attempts": self.attempts,
            "failed_count": len(failed),
            "notes": notes,
        }


HEALTH = HealthLog()


def chain(group: str, providers: list[Provider],
          validate: Callable[[Any], str] | None = None) -> Outcome:
    """按优先级逐个尝试数据源，返回第一个通过校验的结果。

    validate 返回人类可读的数据摘要；拒绝结果时抛 SourceFailure。
    失败不抛给调用方，只记录到 HEALTH，全部失败才抛 AllSourcesFailed。
    """
    attempts: list[dict] = []
    for p in providers:
        t0 = time.perf_counter()
        try:
            value = p.call()
            detail = validate(value) if validate else "ok"
            ms = int((time.perf_counter() - t0) * 1000)
            rec = {"group": group, "provider": p.key, "label": p.label,
                   "endpoint": p.endpoint, "ok": True, "ms": ms, "detail": detail}
            attempts.append(rec)
            HEALTH.add(**rec)
            return Outcome(value, p.key, p.label, p.quality, p.endpoint, attempts)
        except Exception as exc:  # noqa: BLE001 - 单源失败即降级
            ms = int((time.perf_counter() - t0) * 1000)
            rec = {"group": group, "provider": p.key, "label": p.label,
                   "endpoint": p.endpoint, "ok": False, "ms": ms,
                   "detail": f"{type(exc).__name__}: {str(exc)[:120]}"}
            attempts.append(rec)
            HEALTH.add(**rec)
    raise AllSourcesFailed(group, attempts)


# ---------------------------------------------------------------------------
# 统一字段骨架
# ---------------------------------------------------------------------------

def blank_pool_item(code: str, name: str, kind: str) -> dict:
    """所有来源共用的池记录结构；缺失字段保持 None，不做猜测填充。"""
    market = _market_of(code)
    item = {
        "code": code, "name": name, "market": market, "secid": secid(code, market),
        "price": None, "pct": None, "amount": None, "float_mv": None,
        "total_mv": None, "turnover": None, "industry": None,
        "first_seal": None, "last_seal": None, "break_count": 0,
        "seal_amount": None, "boards": 1, "stat": {},
        "origin": None,
    }
    if kind == "ZB":
        item["limit_price"] = None
    if kind == "DT":
        item["limit_down_days"] = 0
        item["open_count"] = 0
    return item


def blank_quote(code: str, name: str = "", source: str = "") -> dict:
    return {
        "code": code, "name": name, "source": source,
        "price": None, "pct": None,
        "main_net": None, "main_pct": None,
        "super_net": None, "super_pct": None,
        "big_net": None, "big_pct": None,
        "mid_net": None, "mid_pct": None,
        "small_net": None, "small_pct": None,
        "main_net_5d": None, "main_pct_5d": None,
        "super_net_5d": None, "super_pct_5d": None,
        "big_net_5d": None, "big_pct_5d": None,
        "mid_net_5d": None, "small_net_5d": None,
        "main_net_10d": None, "main_pct_10d": None,
        "amount": None, "float_mv": None, "total_mv": None,
        "turnover": None, "vol_ratio": None,
        "limit_price": None, "high": None, "low": None,
        "prev_close": None, "quote_time": None,
        "fund_available": False,
    }


# ---------------------------------------------------------------------------
# 供应商 1：东方财富
# ---------------------------------------------------------------------------

def em_fetch_pool(kind: str, day: dt.date, *, page_size: int = 200) -> dict:
    endpoint = {"ZT": "getTopicZTPool", "ZB": "getTopicZBPool",
                "DT": "getTopicDTPool"}[kind]
    sort = {"ZT": "fbt:asc", "ZB": "fbt:asc", "DT": "fund:asc"}[kind]
    target = date_compact(day)
    last: Exception | None = None

    for host in EM_POOL_HOSTS:
        try:
            items: list[dict] = []
            total = 0
            qdate_raw: Any = None
            for page in range(10):
                url = (f"{host}/{endpoint}?ut={UT}&dpt=wz.ztzt&Pageindex={page}"
                       f"&pagesize={page_size}&sort={urllib.parse.quote(sort, safe='')}"
                       f"&date={target}")
                data = http_json(url, referer=EM_REFERER, timeout=9, retries=2)
                block = data.get("data")
                if not isinstance(block, dict):
                    raise SourceFailure(f"{host} 返回 data=null（rc={data.get('rc')}）")
                pool = block.get("pool") or []
                if page == 0:
                    total = int(block.get("tc") or 0)
                    qdate_raw = block.get("qdate")
                for raw in pool:
                    items.append(_em_pool_item(kind, raw))
                if len(pool) < page_size or (total and len(items) >= total):
                    break
                time.sleep(0.12)
            return {
                "qdate": compact_to_iso(qdate_raw) if qdate_raw else date_iso(day),
                "total": total or len(items), "items": items,
                "endpoint": host,
            }
        except Exception as exc:  # noqa: BLE001 - 换下一个域名
            last = exc
    raise SourceFailure(f"东财涨停池多域名均失败: {type(last).__name__} {str(last)[:100]}")


def _em_pool_item(kind: str, r: dict) -> dict:
    code = str(r.get("c") or "")
    market = int(r.get("m") or 0)
    item = blank_pool_item(code, str(r.get("n") or ""), kind)
    item.update({
        "market": market, "secid": secid(code, market),
        "price": (num(r.get("p")) or 0) / 1000,   # 接口价格单位：千分之一元
        "pct": num(r.get("zdp")),
        "amount": num(r.get("amount")),
        "float_mv": num(r.get("ltsz")),
        "total_mv": num(r.get("tshare")),
        "turnover": num(r.get("hs")),
        "industry": str(r.get("hybk") or "未分类"),
        "first_seal": r.get("fbt"),
        "last_seal": r.get("lbt"),
        "break_count": int(num(r.get("zbc")) or 0),
        "seal_amount": num(r.get("fund")),
        "boards": int(num(r.get("lbc")) or 0) or 1,
        "stat": r.get("zttj") or {},
        "origin": "eastmoney",
    })
    if kind == "ZB":
        item["limit_price"] = (num(r.get("ztp")) or 0) / 1000
    if kind == "DT":
        item["limit_down_days"] = int(num(r.get("days")) or 0)
        item["open_count"] = int(num(r.get("oc")) or 0)
    return item


def em_fetch_quotes(secids: list[str], with_flow: bool = True) -> dict[str, dict]:
    """东财批量行情 + 资金拆解，按 host 轮换；单批失败抛错交给上层换供应商。"""
    fields = FLOW_FIELDS if with_flow else QUOTE_FIELDS
    last: Exception | None = None
    for host in EM_QUOTE_HOSTS:
        try:
            url = (f"{host}/api/qt/ulist.np/get?fltt=2&invt=2&ut={UT}"
                   f"&secids={','.join(secids)}&fields={fields}")
            data = http_json(url, referer=EM_REFERER, timeout=10, retries=1)
            rows = ((data.get("data") or {}).get("diff") or [])
            if isinstance(rows, dict):
                rows = list(rows.values())
            if not rows:
                raise SourceFailure(f"{host} 返回空行情")
            out: dict[str, dict] = {}
            for r in rows:
                q = _em_row_to_quote(r, with_flow, "eastmoney")
                if q:
                    out[q["code"]] = q
            if not out:
                raise SourceFailure(f"{host} 返回行情无法解析")
            return out
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise SourceFailure(f"东财行情多域名均失败: {type(last).__name__} {str(last)[:100]}")


def _em_row_to_quote(r: dict, with_flow: bool, tag: str) -> dict | None:
    """ulist.np 与 clist 共用同一套 f 字段，归一化逻辑只维护一份。"""
    code = str(r.get("f12") or "")
    if not code:
        return None
    q = blank_quote(code, str(r.get("f14") or ""), tag)
    q.update({
        "price": num(r.get("f2")), "pct": num(r.get("f3")),
        "main_net": num(r.get("f62")), "main_pct": num(r.get("f184")),
        "super_net": num(r.get("f66")), "super_pct": num(r.get("f69")),
        "big_net": num(r.get("f72")), "big_pct": num(r.get("f75")),
        "mid_net": num(r.get("f78")), "mid_pct": num(r.get("f81")),
        "small_net": num(r.get("f84")), "small_pct": num(r.get("f87")),
        "main_net_5d": num(r.get("f164")), "main_pct_5d": num(r.get("f165")),
        "super_net_5d": num(r.get("f166")), "super_pct_5d": num(r.get("f167")),
        "big_net_5d": num(r.get("f168")), "big_pct_5d": num(r.get("f169")),
        "mid_net_5d": num(r.get("f170")), "small_net_5d": num(r.get("f172")),
        "main_net_10d": num(r.get("f174")), "main_pct_10d": num(r.get("f175")),
        "amount": num(r.get("f6")), "turnover": num(r.get("f8")),
        "vol_ratio": num(r.get("f10")), "total_mv": num(r.get("f20")),
        "float_mv": num(r.get("f21")), "industry": r.get("f100"),
        "quote_time": _ts_to_bj(r.get("f124")),
        "fund_available": with_flow and num(r.get("f62")) is not None,
        "fund_source": "eastmoney",
    })
    return q


def em_fetch_quotes_clist(secids: list[str], with_flow: bool = True,
                          max_pages: int = 6) -> dict[str, dict]:
    """东财第二条批量通道：全市场涨幅榜（clist）按代码匹配。

    实测 ulist.np 被限流时 clist 仍可用，且返回完全相同的 f 字段与口径。
    涨停股按涨幅降序必在榜单前部，通常第 1 页即可全部命中；页尾涨幅
    低于 7% 时提前停止，避免无谓翻页。
    """
    wanted = {s.split(".")[-1] for s in secids}
    fields = (FLOW_FIELDS if with_flow else QUOTE_FIELDS) + ",f100"
    last: Exception | None = None
    for host in EM_QUOTE_HOSTS:
        try:
            out: dict[str, dict] = {}
            for page in range(1, max_pages + 1):
                url = (f"{host}/api/qt/clist/get?pn={page}&pz=100&po=1&np=1"
                       f"&fltt=2&invt=2&ut={UT}&fid=f3&fs={EM_CLIST_FS}"
                       f"&fields={fields}")
                data = http_json(url, referer=EM_REFERER, timeout=9, retries=1)
                rows = ((data.get("data") or {}).get("diff") or [])
                if isinstance(rows, dict):
                    rows = list(rows.values())
                if not rows:
                    break
                for r in rows:
                    if str(r.get("f12") or "") in wanted:
                        q = _em_row_to_quote(r, with_flow, "eastmoney_clist")
                        if q:
                            out[q["code"]] = q
                tail = num(rows[-1].get("f3"))
                if wanted.issubset(out) or len(rows) < 100 or (tail is not None and tail < 7):
                    break
                time.sleep(0.1)
            if not out:
                raise SourceFailure(f"{host} 涨幅榜未匹配到目标代码")
            return out
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise SourceFailure(f"东财榜单通道多域名均失败: {type(last).__name__} {str(last)[:100]}")


def _ts_to_bj(value: Any) -> str | None:
    ts = num(value)
    if not ts or ts < 1e9:
        return None
    try:
        return dt.datetime.fromtimestamp(ts, CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError, OverflowError):
        return None


def em_fetch_flow_history(secid_: str, limit: int = 15) -> list[dict]:
    query = (f"api/qt/stock/fflow/daykline/get?lmt={limit}&klt=101&secid={secid_}"
             f"&fields1=f1,f2,f3,f7"
             f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
             f"&ut={UT_HIS}")
    last: Exception | None = None
    for host in EM_HISTORY_HOSTS:
        try:
            data = http_json(f"{host}/{query}", referer=EM_REFERER,
                             timeout=9, retries=1)
            klines = ((data.get("data") or {}).get("klines") or [])
            if not klines:
                raise SourceFailure(f"{host} 返回空资金流日线")
            rows = []
            for line in klines:
                p = str(line).split(",")
                if len(p) < 13:
                    continue
                rows.append({
                    "date": p[0], "main_net": num(p[1]), "small_net": num(p[2]),
                    "mid_net": num(p[3]), "big_net": num(p[4]),
                    "super_net": num(p[5]), "main_pct": num(p[6]),
                    "close": num(p[11]), "pct": num(p[12]),
                })
            return rows[-limit:]
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise SourceFailure(f"东财逐日资金流多域名均失败: {type(last).__name__}")


# ---------------------------------------------------------------------------
# 供应商 2：同花顺（涨停池 + 连板高度）
# ---------------------------------------------------------------------------

def ths_fetch_limit_up(day: dt.date) -> dict:
    url = (f"{THS_API}/limit_up/limit_up_pool?page=1&limit=200"
           f"&field={THS_FIELDS}&filter=HS,GEM2STAR"
           f"&order_field=330324&order_type=0&date={date_compact(day)}")
    data = http_json(url, referer=THS_REFERER, timeout=9, retries=1)
    if data.get("status_code") != 0:
        raise SourceFailure(f"同花顺返回 status_code={data.get('status_code')}")
    block = data.get("data") or {}
    info = block.get("info") or []
    if not info:
        raise SourceFailure("同花顺涨停池为空")
    page = block.get("page") or {}
    total = int(page.get("total") or len(info))

    boards_map: dict[str, int] = {}
    try:
        boards_map = ths_continuous_boards(day)
    except SourceFailure:
        pass  # 连板高度是增强项，拿不到时按首板计并标注

    items = []
    for r in info:
        code = str(r.get("code") or "")
        name = str(r.get("name") or "")
        if not code:
            continue
        item = blank_pool_item(code, name, "ZT")
        fmv = num(r.get("currency_value"))
        turn = num(r.get("turnover_rate"))
        # 同花顺不直接给成交额；换手率定义即成交量/流通股本，故可由市值推导。
        # 该值为推导值，会在 source_health.notes 明确标注。
        amount = (fmv * turn / 100) if (fmv and turn) else None
        topic = str(r.get("reason_type") or "").split("+")[0].strip()
        item.update({
            "price": num(r.get("latest")), "pct": num(r.get("change_rate")),
            "amount": amount, "float_mv": fmv, "total_mv": None,
            "turnover": turn,
            "industry": topic or "未分类",
            "first_seal": _epoch_hhmmss(r.get("first_limit_up_time")),
            "last_seal": _epoch_hhmmss(r.get("last_limit_up_time")),
            "break_count": int(num(r.get("open_num")) or 0),
            "seal_amount": num(r.get("order_amount")),
            "boards": boards_map.get(code) or _parse_high_days(r.get("high_days")),
            "origin": "ths", "amount_estimated": amount is not None,
        })
        items.append(item)

    return {"qdate": date_iso(day), "total": total or len(items),
            "items": items, "endpoint": THS_API,
            "boards_enriched": bool(boards_map)}


def ths_continuous_boards(day: dt.date) -> dict[str, int]:
    url = f"{THS_API}/limit_up/continuous_limit_up?date={date_compact(day)}"
    data = http_json(url, referer=THS_REFERER, timeout=7, retries=1)
    rows = data.get("data") or []
    out: dict[str, int] = {}
    for group in rows if isinstance(rows, list) else []:
        for one in group.get("code_list") or []:
            code = str(one.get("code") or "")
            n = int(num(one.get("continue_num")) or num(group.get("height")) or 1)
            if code:
                out[code] = max(1, n)
    if not out:
        raise SourceFailure("同花顺连板池为空")
    return out


def _epoch_hhmmss(value: Any) -> int | None:
    ts = num(value)
    if not ts or ts < 1e9:
        return None
    try:
        return int(dt.datetime.fromtimestamp(ts, CN_TZ).strftime("%H%M%S"))
    except (OSError, ValueError, OverflowError):
        return None


def _parse_high_days(value: Any) -> int:
    text = str(value or "")
    m = re.search(r"(\d+)\s*连板", text)
    return max(1, int(m.group(1))) if m else 1


# ---------------------------------------------------------------------------
# 供应商 3：新浪财经（全市场榜单 → 重建池；逐票行情）
# ---------------------------------------------------------------------------

def sina_rank(asc: bool, day: dt.date, *, max_pages: int = 6,
              page_size: int = 100) -> list[dict]:
    key = (date_iso(day), asc)
    cached = _SINA_RANK_CACHE.get(key)
    if cached and time.time() - cached[0] < 300:
        return cached[1]
    rows: list[dict] = []
    for page in range(1, max_pages + 1):
        url = (f"{SINA_RANK}?page={page}&num={page_size}&sort=changepercent"
               f"&asc={1 if asc else 0}&node=hs_a")
        text = http_text(url, referer=SINA_REFERER, timeout=9, retries=2)
        try:
            batch = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SourceFailure(f"新浪榜单非 JSON: {text[:50]}") from exc
        if not isinstance(batch, list) or not batch:
            break
        rows.extend(batch)
        tail = num(batch[-1].get("changepercent")) or 0
        if len(batch) < page_size or (tail > -7 if asc else tail < 7):
            break
        time.sleep(0.12)
    if not rows:
        raise SourceFailure("新浪全市场榜单为空")
    _SINA_RANK_CACHE[key] = (time.time(), rows)
    return rows


def sina_fetch_pool(kind: str, day: dt.date) -> dict:
    """用全市场涨跌幅榜重建涨停/炸板/跌停池。

    涨停价 = 昨收 ×(1+涨跌幅限制)，四舍五入到分；触及涨停价但现价低于它
    视为炸板。此源只能提供量价信息，封板时间/封单额/连板数一律为 None，
    由评分层的中性 50 分兜底，绝不伪造。
    """
    up_rows = sina_rank(False, day)
    down_rows = sina_rank(True, day) if kind == "DT" else []
    source_rows = down_rows if kind == "DT" else up_rows
    items: list[dict] = []

    for r in source_rows:
        code = str(r.get("code") or "")
        name = str(r.get("name") or "")
        if not code or not name:
            continue
        prev = num(r.get("settlement"))
        price = num(r.get("trade"))
        high = num(r.get("high"))
        pct = num(r.get("changepercent"))
        if not prev or price is None or pct is None or prev <= 0:
            continue
        p = limit_pct(code, name)
        upper = _limit_price(prev, p)
        lower = _limit_price(prev, -p)
        tol = 0.011
        sealed_up = price >= upper - tol
        touched_up = high is not None and high >= upper - tol
        sealed_down = price <= lower + tol

        hit = (sealed_up if kind == "ZT" else
               (touched_up and not sealed_up) if kind == "ZB" else sealed_down)
        if not hit:
            continue

        item = blank_pool_item(code, name, kind)
        nmc = num(r.get("nmc"))
        mktcap = num(r.get("mktcap"))
        item.update({
            "price": price, "pct": pct,
            "amount": num(r.get("amount")),
            "float_mv": nmc * 1e4 if nmc is not None else None,
            "total_mv": mktcap * 1e4 if mktcap is not None else None,
            "turnover": num(r.get("turnoverratio")),
            "industry": "未分类",
            "seal_amount": None, "first_seal": None, "last_seal": None,
            "boards": 1, "break_count": 0,
            "origin": "sina",
            "quote_time": f"{date_iso(day)} {r.get('ticktime') or ''}".strip(),
        })
        if kind == "ZB":
            item["limit_price"] = upper
        items.append(item)

    if not items and kind != "DT":
        raise SourceFailure(f"新浪榜单未重建出{kind}池")
    return {"qdate": date_iso(day), "total": len(items), "items": items,
            "endpoint": SINA_RANK}


def sina_fetch_quotes(codes: list[str]) -> dict[str, dict]:
    symbols = ",".join(_tx_symbol(c) for c in codes)
    text = http_text(SINA_HQ + symbols, referer=SINA_REFERER,
                     encoding="gbk", timeout=9, retries=2)
    out: dict[str, dict] = {}
    for seg in text.split(";"):
        m = re.search(r'hq_str_\w+="(.*)"', seg)
        if not m:
            continue
        p = m.group(1).split(",")
        if len(p) < 10:
            continue
        code = re.search(r"hq_str_\w{2}(\d{6})", seg)
        if not code:
            continue
        c = code.group(1)
        price, prev = num(p[3]), num(p[2])
        q = blank_quote(c, p[0], "sina")
        q.update({
            "price": price, "prev_close": prev,
            "pct": round((price - prev) / prev * 100, 2)
                   if (price is not None and prev) else None,
            "amount": num(p[9]), "high": num(p[4]), "low": num(p[5]),
            "quote_time": f"{p[30]} {p[31]}" if len(p) > 31 else None,
        })
        out[c] = q
    if not out:
        raise SourceFailure("新浪逐票行情为空")
    return out


def sina_fetch_flow(codes: list[str], batch: int = 30) -> dict[str, dict]:
    """新浪单股资金流（支持一次传多个 daima），用于补齐当日四类单拆解。

    口径差异（重要）：新浪 r0~r3 是「主动买入 / 主动卖出」按单笔金额分级，
    与东方财富「大单+超大单」口径不完全等价，数值不可跨源直接比较。
    因此结果会打上 ``fund_source=sina`` / ``fund_comparable=False``，
    看板与飞书卡片必须显式提示，避免与历史分数误对比。

    r0=超大单, r1=大单, r2=中单, r3=小单；净额 = in - out；
    成交额用 volume(股) × trade(元) 推算，仅作占比分母。
    """
    out: dict[str, dict] = {}
    for i in range(0, len(codes), batch):
        chunk = codes[i:i + batch]
        symbols = ",".join(_tx_symbol(c) for c in chunk)
        try:
            text = http_text(f"{SINA_FLOW}?daima={symbols}",
                             referer=SINA_REFERER, timeout=9, retries=1)
            data = json.loads(text)
        except (json.JSONDecodeError, SourceFailure):
            continue
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            continue
        for idx, r in enumerate(data):
            symbol = str(r.get("symbol") or "")
            code = symbol[-6:] if re.fullmatch(r"\w{2}\d{6}", symbol) else \
                (chunk[idx] if idx < len(chunk) else "")
            if not re.fullmatch(r"\d{6}", code):
                continue
            amount = (num(r.get("volume")) or 0) * (num(r.get("trade")) or 0) or None
            nets = {}
            for key, field in (("super", "r0"), ("big", "r1"),
                               ("mid", "r2"), ("small", "r3")):
                nets[key] = (num(r.get(field + "_in")) or 0) - (num(r.get(field + "_out")) or 0)
            main = nets["super"] + nets["big"]
            pct = (lambda v: round(v / amount * 100, 2) if amount else None)
            out[code] = {
                "main_net": main, "main_pct": pct(main),
                "super_net": nets["super"], "super_pct": pct(nets["super"]),
                "big_net": nets["big"], "big_pct": pct(nets["big"]),
                "mid_net": nets["mid"], "mid_pct": pct(nets["mid"]),
                "small_net": nets["small"], "small_pct": pct(nets["small"]),
                "fund_available": True, "fund_source": "sina",
                "fund_comparable": False,
            }
        if i + batch < len(codes):
            time.sleep(0.1)
    if not out:
        raise SourceFailure("新浪资金流为空")
    return out


def apply_flow_patch(quotes: dict[str, dict], patch: dict[str, dict]) -> int:
    """把备用资金口径写入已有行情记录（只覆盖资金字段，保留量价来源）。"""
    n = 0
    for code, fields in patch.items():
        q = quotes.get(code)
        if not q or q.get("fund_available"):
            continue
        q.update(fields)
        n += 1
    return n


def sina_quotes_from_rank(day: dt.date, codes: list[str]) -> dict[str, dict]:
    """池已由新浪榜单重建时直接复用榜单字段，省掉一次逐票请求。"""
    want = set(codes)
    out: dict[str, dict] = {}
    for asc in (False, True):
        for r in _SINA_RANK_CACHE.get((date_iso(day), asc), (0, []))[1]:
            code = str(r.get("code") or "")
            if code not in want or code in out:
                continue
            nmc, mktcap = num(r.get("nmc")), num(r.get("mktcap"))
            q = blank_quote(code, str(r.get("name") or ""), "sina")
            q.update({
                "price": num(r.get("trade")), "pct": num(r.get("changepercent")),
                "prev_close": num(r.get("settlement")),
                "high": num(r.get("high")), "low": num(r.get("low")),
                "amount": num(r.get("amount")),
                "float_mv": nmc * 1e4 if nmc is not None else None,
                "total_mv": mktcap * 1e4 if mktcap is not None else None,
                "turnover": num(r.get("turnoverratio")),
                "quote_time": f"{date_iso(day)} {r.get('ticktime') or ''}".strip(),
            })
            out[code] = q
    if not out:
        raise SourceFailure("榜单缓存中没有目标代码")
    return out


# ---------------------------------------------------------------------------
# 供应商 4：腾讯证券（逐票行情，含量比/市值/涨跌停价，无资金拆解）
# ---------------------------------------------------------------------------

def tencent_fetch_quotes(codes: list[str]) -> dict[str, dict]:
    symbols = ",".join(_tx_symbol(c) for c in codes)
    text = http_text(TENCENT_HQ + symbols, referer=TENCENT_REFERER,
                     encoding="gbk", timeout=9, retries=2)
    out: dict[str, dict] = {}
    for seg in text.split(";"):
        if "=" not in seg or "~" not in seg:
            continue
        p = seg.split("=", 1)[1].strip().strip('"').split("~")
        if len(p) < 50 or not re.fullmatch(r"\d{6}", p[2] or ""):
            continue
        code = p[2]
        q = blank_quote(code, p[1], "tencent")
        amount_wan = _at(p, 37)      # 成交额：万元
        fmv_yi = _at(p, 44)          # 流通市值：亿元
        tmv_yi = _at(p, 45)          # 总市值：亿元
        q.update({
            "price": _at(p, 3), "prev_close": _at(p, 4),
            "pct": _at(p, 32), "high": _at(p, 33), "low": _at(p, 34),
            "amount": amount_wan * 1e4 if amount_wan is not None else None,
            "float_mv": fmv_yi * 1e8 if fmv_yi is not None else None,
            "total_mv": tmv_yi * 1e8 if tmv_yi is not None else None,
            "turnover": _at(p, 38), "vol_ratio": _at(p, 49),
            "limit_price": _at(p, 47), "quote_time": p[30] if len(p) > 30 else None,
        })
        out[code] = q
    if not out:
        raise SourceFailure("腾讯行情为空或格式变化（v_pv_none_match）")
    return out


# ---------------------------------------------------------------------------
# 对外组合接口
# ---------------------------------------------------------------------------

def _pool_providers(kind: str, day: dt.date, cfg: dict) -> list[Provider]:
    sc = cfg.get("data_sources", {})
    out: list[Provider] = []
    if sc.get("eastmoney", True):
        out.append(Provider("eastmoney", "东方财富涨停池", "full",
                            EM_POOL_HOSTS[0],
                            lambda: em_fetch_pool(kind, day)))
    if kind == "ZT" and sc.get("ths", True):
        out.append(Provider("ths", "同花顺涨停池", "partial",
                            "data.10jqka.com.cn/dataapi/limit_up/limit_up_pool",
                            lambda: ths_fetch_limit_up(day)))
    if sc.get("sina", True):
        out.append(Provider("sina", "新浪财经全市场榜单", "minimal",
                            "vip.stock.finance.sina.com.cn · Market_Center",
                            lambda: sina_fetch_pool(kind, day)))
    return out


def _quote_providers(secids: list[str], day: dt.date, cfg: dict,
                     with_flow: bool) -> list[Provider]:
    sc = cfg.get("data_sources", {})
    codes = [s.split(".")[-1] for s in secids]
    out: list[Provider] = []
    if sc.get("eastmoney", True):
        out.append(Provider("eastmoney", "东方财富行情＋资金流", "full",
                            EM_QUOTE_HOSTS[0] + " · ulist.np",
                            lambda: em_fetch_quotes(secids, with_flow)))
    if sc.get("eastmoney_clist", True):
        out.append(Provider("eastmoney_clist", "东方财富榜单通道（同口径）", "full",
                            EM_QUOTE_HOSTS[0] + " · clist",
                            lambda: em_fetch_quotes_clist(secids, with_flow)))
    if sc.get("tencent", True):
        out.append(Provider("tencent", "腾讯证券行情", "partial",
                            "qt.gtimg.cn", lambda: tencent_fetch_quotes(codes)))
    if sc.get("sina", True):
        try:
            sina_quotes_from_rank(day, codes)
            out.append(Provider("sina", "新浪榜单复用（量价）", "minimal",
                                "新浪全市场榜单缓存",
                                lambda: sina_quotes_from_rank(day, codes)))
        except SourceFailure:
            pass
        out.append(Provider("sina", "新浪财经逐票行情", "minimal",
                            "hq.sinajs.cn", lambda: sina_fetch_quotes(codes)))
    return out


def _pool_validate(kind: str):
    def _v(value: dict) -> str:
        items = value.get("items") or []
        # 空池可能是真实的（极端行情），但 data 结构必须完整；这里只校验结构
        if "total" not in value or "qdate" not in value:
            raise SourceFailure("池数据结构不完整")
        return f"{len(items)} 只 / 行情日 {value['qdate']}"
    return _v


def fetch_pool(kind: str, day: dt.date, cfg: dict | None = None) -> tuple[dict, Outcome]:
    cfg = cfg or {}
    outcome = chain(f"pool:{kind}", _pool_providers(kind, day, cfg),
                    _pool_validate(kind))
    return outcome.value, outcome


def fetch_quotes(secids: list[str], day: dt.date, cfg: dict | None = None,
                 with_flow: bool = True, batch: int = 45) -> tuple[dict, dict]:
    """分批取行情；每批独立降级，单批失败不拖垮其它批。"""
    cfg = cfg or {}
    merged: dict[str, dict] = {}
    providers_used: dict[str, int] = {}
    attempts: list[dict] = []
    for i in range(0, len(secids), batch):
        chunk = secids[i:i + batch]
        outcome = chain("quotes", _quote_providers(chunk, day, cfg, with_flow),
                        lambda v: f"{len(v)} 只")
        merged.update(outcome.value)
        providers_used[outcome.label] = providers_used.get(outcome.label, 0) + len(outcome.value)
        attempts.extend(outcome.attempts)
        if i + batch < len(secids):
            time.sleep(0.1)
    # 资金拆解补齐：主源/榜单通道都拿不到时，用新浪批量资金流补当日四类单。
    # 该源口径与东财不同，会显式打标，绝不静默混用。
    missing = [c for c, v in merged.items() if not v.get("fund_available")]
    filled = 0
    fund_source = "eastmoney" if any(v.get("fund_available") for v in merged.values()) else "none"
    sc = cfg.get("data_sources", {})
    if missing and sc.get("sina_flow_backfill", True):
        t0 = time.perf_counter()
        try:
            patch = sina_fetch_flow(missing)
            filled = apply_flow_patch(merged, patch)
            detail = f"补齐 {filled}/{len(missing)} 只当日资金拆解（新浪口径）"
            ok = True
            if filled:
                fund_source = "sina" if fund_source == "none" else "mixed"
        except Exception as exc:  # noqa: BLE001
            detail, ok = f"{type(exc).__name__}: {str(exc)[:100]}", False
        rec = {"group": "flow_backfill", "provider": "sina_flow",
               "label": "新浪资金流补齐", "endpoint": SINA_FLOW,
               "ok": ok, "ms": int((time.perf_counter() - t0) * 1000),
               "detail": detail}
        attempts.append(rec)
        HEALTH.add(**rec)

    meta = {
        "providers": providers_used,
        "coverage": f"{len(merged)}/{len(secids)}",
        "has_fund": any(v.get("fund_available") for v in merged.values()),
        "fund_coverage": sum(1 for v in merged.values() if v.get("fund_available")),
        "fund_source": fund_source,
        "fund_backfilled": filled,
        "fund_comparable": fund_source == "eastmoney",
        "attempts": attempts,
    }
    return merged, meta


def fetch_flow_history(secid_: str, cfg: dict | None = None,
                       limit: int = 15) -> list[dict]:
    cfg = cfg or {}
    providers: list[Provider] = []
    if cfg.get("data_sources", {}).get("eastmoney", True):
        providers.append(Provider("eastmoney", "东方财富逐日资金流", "full",
                                  EM_HISTORY_HOSTS[0],
                                  lambda: em_fetch_flow_history(secid_, limit)))
    if not providers:
        return []
    try:
        return chain("flow_history", providers,
                     lambda v: f"{len(v)} 个交易日").value
    except AllSourcesFailed:
        return []


def cross_check_limit_up(day: dt.date, primary_items: list[dict],
                         cfg: dict) -> dict:
    """主源与同花顺交叉校验涨停家数与代码重合度（不阻塞主流程）。"""
    if not cfg.get("data_sources", {}).get("cross_check", True):
        return {"available": False, "reason": "已在 config.json 关闭"}
    try:
        other = ths_fetch_limit_up(day)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"同花顺校验接口不可用：{type(exc).__name__}"}
    a = {x["code"] for x in primary_items}
    b = {x["code"] for x in other["items"]}
    overlap = len(a & b) / max(1, len(a | b))
    return {
        "available": True,
        "primary_count": len(primary_items),
        "ths_count": len(other["items"]),
        "overlap_rate": round(overlap * 100, 1),
        "note": ("两源家数与成分一致，数据可信" if overlap >= 0.9 and
                 abs(len(a) - len(b)) <= max(2, len(a) * 0.1) else
                 f"两源存在差异（重合 {overlap * 100:.0f}%），已保留主源数据并在看板标注"),
    }


def describe_sources(pools: dict[str, Outcome], quotes_meta: dict) -> str:
    pool_names = []
    for kind in ("ZT", "ZB", "DT"):
        o = pools.get(kind)
        if o and o.label not in pool_names:
            pool_names.append(o.label)
    quote_names = list(quotes_meta.get("providers", {}).keys())
    parts = ["＋".join(pool_names) if pool_names else "无池数据",
             "＋".join(quote_names) if quote_names else "无行情"]
    fund = quotes_meta.get("fund_source")
    if fund == "sina":
        parts.append("新浪资金流（口径已切换）")
    elif fund == "mixed":
        parts.append("资金部分由新浪补齐（混合口径）")
    elif fund == "none":
        parts.append("本期无资金拆解")
    return " / ".join(parts)


def quality_of(pools: dict[str, Outcome], quotes_meta: dict) -> str:
    pool_q = [o.quality for o in pools.values() if o]
    has_fund = quotes_meta.get("has_fund")
    comparable = quotes_meta.get("fund_comparable", True)
    if all(q == "full" for q in pool_q) and has_fund and comparable:
        return "full"
    if any(q == "minimal" for q in pool_q) and not has_fund:
        return "minimal"
    return "partial"


def build_notes(pools: dict[str, Outcome], quotes_meta: dict,
                cross: dict, quality: str) -> list[str]:
    notes: list[str] = []
    for kind, o in pools.items():
        if o and o.provider != "eastmoney":
            notes.append(f"{kind} 池已降级到「{o.label}」（{o.endpoint}）")
    used = quotes_meta.get("providers", {})
    backfilled = quotes_meta.get("fund_backfilled", 0) > 0
    for label in used:
        if "东方财富" not in label:
            suffix = ("；当日四类单已由新浪资金流补齐" if backfilled
                      else "，资金因子按中性 50 分计")
            notes.append(f"部分行情来自「{label}」，该源不含资金拆解{suffix}")
    if not quotes_meta.get("has_fund"):
        notes.append("本期未取到任何主力资金拆解，资金流排行与个股资金图表为空，评分仅基于量价与封板质量")
    if quotes_meta.get("fund_source") in ("sina", "mixed"):
        notes.append("资金拆解已切换到新浪「主动买卖单」口径，与东方财富「大单+超大单」口径不等价，"
                     "跨日分数不可直接比较；且该源不提供 5 日/10 日多周期数据")
    if any((i.get("amount_estimated") for o in pools.values()
            if o for i in o.value.get("items", []))):
        notes.append("同花顺源不提供成交额，按「流通市值 × 换手率」推导，仅用于流动性门槛筛选")
    sina_kinds = [k for k, o in pools.items() if o and o.provider == "sina"]
    if sina_kinds:
        notes.append(f"{'/'.join(sina_kinds)} 池由新浪榜单按涨跌停价重建，"
                     "无封板时间、封单额、连板数与行业，相关因子按中性处理，跨日分数不可直接比较")
    if cross.get("available") and cross.get("overlap_rate", 100) < 90:
        notes.append(f"交叉校验：{cross['note']}")
    if quality == "full":
        notes.append("全部主源可用，数据口径与历史快照一致")
    return notes
