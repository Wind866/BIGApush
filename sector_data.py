"""
板块数据获取模块
获取行业板块、概念板块、资金流向数据
"""
import requests
import re
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import biga_sources as bs  # noqa: E402  板块榜备用源（东财多域名）


def _board_fallback(kind: str):
    """新浪板块接口失效时，用东财板块榜（多域名）重建涨跌列表。"""
    try:
        rows = bs.fetch_board_em(kind, limit=100)
        out = [{"name": r["name"], "pct": r["pct"] or 0.0}
               for r in rows if r["name"] and r["pct"] is not None]
        out.sort(key=lambda x: x["pct"], reverse=True)
        print(f"[数据源] {kind} 板块：东财板块榜（新浪接口降级）")
        return out
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ {kind} 板块东财源不可用：{type(exc).__name__} {exc}")
        return _tencent_board(kind)


# 腾讯板块榜：数据中心 IP 友好（本项目行情/指数一直走腾讯），无需 token。
# board_type: hy=行业, gn=概念；zdf=涨跌幅(%)，zljlr=主力净流入。
_TENCENT_BOARD_URL = (
    "https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank"
    "?board_type={kind}&sort_type=price&direct=down&offset={offset}&count=100&type=1"
)


def _f(value, default=None):
    """把腾讯返回的字符串数字转 float，失败给默认值。"""
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _tencent_board(kind: str, max_pages: int = 8):
    """腾讯板块榜全量拉取（每页 100，翻页直到空）。

    腾讯按页码给的是"指数点位"序，不是涨跌幅序，所以必须自己排序，
    否则领涨/领跌会取错（早期只取第一页直接当排序结果就会错）。
    """
    rows, seen = [], set()
    for page in range(max_pages):
        url = _TENCENT_BOARD_URL.format(kind=kind, offset=page * 100)
        try:
            resp = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ 腾讯 {kind} 板块第 {page + 1} 页失败：{type(exc).__name__} {exc}")
            break
        items = (data.get("data") or {}).get("rank_list") or []
        if not items:
            break
        for it in items:
            name = str(it.get("name") or "").strip()
            pct = _f(it.get("zdf"))
            if name and pct is not None and name not in seen:
                seen.add(name)
                rows.append({"name": name, "pct": pct})
        if len(items) < 100:
            break
    rows.sort(key=lambda x: x["pct"], reverse=True)
    if rows:
        print(f"[数据源] {kind} 板块：腾讯板块榜（{len(rows)} 个）")
    return rows


def _fund_flow_fallback():
    """新浪板块资金流失效时，用东财板块主力净额榜降级。"""
    try:
        rows = bs.fetch_board_em("industry", sort_field="f62", limit=100)
        inflow = [{"name": r["name"], "fund": r["main_net"] / 1e8}
                  for r in rows if r["name"] and r["main_net"] and r["main_net"] > 0][:5]
        outflow = sorted(
            [{"name": r["name"], "fund": r["main_net"] / 1e8}
             for r in rows if r["name"] and r["main_net"] and r["main_net"] < 0],
            key=lambda x: x["fund"])[:5]
        print("[数据源] 板块资金：东财主力净额榜（新浪接口降级）")
        return inflow, outflow
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 板块资金全部数据源不可用：{type(exc).__name__} {exc}")
        return [], []


def get_industry_sectors():
    """获取行业板块涨跌"""
    try:
        url = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
        resp = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        resp.encoding = "gbk"  # 新浪板块接口返回 GBK，不指定会乱码
        match = re.search(r"=\s*(\{.*\})", resp.text, re.DOTALL)
        if match:
            data = json.loads(match.group(1))
            sectors = []
            for v in data.values():
                parts = v.split(",")
                if len(parts) > 5:
                    name = parts[1]
                    pct = float(parts[4]) if parts[4] else 0
                    sectors.append({"name": name, "pct": pct})
            sectors.sort(key=lambda x: x["pct"], reverse=True)
            if sectors:
                return sectors
    except Exception:
        pass
    return _board_fallback("industry")


def _concept_from_tencent():
    """腾讯概念板块榜（主源）：数据中心 IP 友好，无需 token。"""
    rows = _tencent_board("gn")
    if rows:
        return rows
    print("⚠️ 概念板块：腾讯板块榜返回空")
    return []


def _concept_from_eastmoney():
    """东财概念板块榜（多域名轮换）。现在是概念板块的主源。"""
    try:
        rows = bs.fetch_board_em("concept", limit=100)
        out = [{"name": r["name"], "pct": r["pct"] or 0.0}
               for r in rows if r["name"] and r["pct"] is not None]
        out.sort(key=lambda x: x["pct"], reverse=True)
        if out:
            print(f"[数据源] 概念板块：东财板块榜（{len(out)} 个）")
            return out
        print("⚠️ 概念板块：东财返回空列表")
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 概念板块东财源不可用：{type(exc).__name__} {exc}")
    return []


def _concept_from_sina():
    """新浪概念板块接口（newSinaGN.php）。

    2026-11 实测该接口已 **返回 404 下线**，所以它不再是主源，只作"万一恢复"的最后尝试。
    字段位置与行业接口（newSinaHy.php）一致：parts[1]=名称、**parts[4]=涨跌幅**；
    原实现取 parts[3]（均价）当涨跌幅，接口一旦恢复就会把均价当涨幅发出去。
    """
    try:
        url = "https://vip.stock.finance.sina.com.cn/q/view/newSinaGN.php"
        resp = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}（接口已下线）")
        resp.encoding = "gbk"  # 新浪板块接口返回 GBK，不指定会乱码
        match = re.search(r"=\s*(\{.*\})", resp.text, re.DOTALL)
        if match:
            data = json.loads(match.group(1))
            sectors = []
            for v in data.values():
                parts = v.split(",")
                if len(parts) > 4:
                    name = parts[1]
                    pct = float(parts[4]) if parts[4] else 0
                    sectors.append({"name": name, "pct": pct})
            sectors.sort(key=lambda x: x["pct"], reverse=True)
            if sectors:
                print(f"[数据源] 概念板块：新浪 newSinaGN（{len(sectors)} 个）")
                return sectors
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 概念板块新浪源不可用：{type(exc).__name__} {exc}")
    return []


def get_concept_sectors():
    """获取概念板块涨跌（腾讯为主 → 东财 → 新浪最后尝试）

    历史上这里以新浪 newSinaGN.php 为主源、东财为兜底；该接口下线（实测 404）后
    实际只剩东财一条路，取不到时整段概念板块会从推送消息里静默消失。
    现在先打腾讯板块榜（与行情同源、数据中心可访问），再试东财，最后试新浪；
    全失败就返回空列表，由 format_sector_message 显式写出「数据源不可用」。
    """
    return _concept_from_tencent() or _concept_from_eastmoney() or _concept_from_sina()


def get_sector_fund_flow():
    """获取板块资金流向（新浪）"""
    try:
        url = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_bkzj_bk"
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}
        
        # 获取流入最多（降序）
        resp1 = requests.get(url, params={"page": 1, "num": 5, "sort": "netamount", "asc": 0, "fenlei": 1}, timeout=8, headers=headers)
        inflow_data = resp1.json() if resp1.status_code == 200 else []
        
        # 获取流出最多（升序）
        resp2 = requests.get(url, params={"page": 1, "num": 5, "sort": "netamount", "asc": 1, "fenlei": 1}, timeout=8, headers=headers)
        outflow_data = resp2.json() if resp2.status_code == 200 else []
        
        inflow = []
        for item in inflow_data:
            name = item.get("name", "")
            net = float(item.get("netamount", 0)) / 100000000
            if net > 0:
                inflow.append({"name": name, "fund": net})
        
        outflow = []
        for item in outflow_data:
            name = item.get("name", "")
            net = float(item.get("netamount", 0)) / 100000000
            if net < 0:
                outflow.append({"name": name, "fund": net})
        
        if inflow or outflow:
            return inflow, outflow
    except Exception:
        pass
    return _fund_flow_fallback()


def format_sector_message():
    """格式化板块数据为飞书消息"""
    lines = []

    # 行业板块
    industries = get_industry_sectors()
    if industries:
        up = [f"{s['name']}({s['pct']:+.2f}%)" for s in industries[:5]]
        dn = [f"{s['name']}({s['pct']:+.2f}%)" for s in industries[-5:]]
        lines.append("📊 行业板块")
        lines.append(f"  🔴 领涨: {' | '.join(up)}")
        lines.append(f"  🟢 领跌: {' | '.join(dn)}")
        lines.append("")

    # 概念板块
    concepts = get_concept_sectors()
    if concepts:
        up = [f"{s['name']}({s['pct']:+.2f}%)" for s in concepts[:5]]
        dn = [f"{s['name']}({s['pct']:+.2f}%)" for s in concepts[-5:]]
        lines.append("💡 概念板块")
        lines.append(f"  🔴 领涨: {' | '.join(up)}")
        lines.append(f"  🟢 领跌: {' | '.join(dn)}")
        lines.append("")
    else:
        # 缺数据必须看得见：以前这里是 if 为假就整段跳过，
        # 消息看起来"完整"，实际上概念板块已经连续多天没推出来。
        lines.append("💡 概念板块")
        lines.append("  ⚠️ 数据源暂不可用（东财/新浪均失败），本段缺失")
        lines.append("")

    # 资金流向
    lines.append("💰 板块资金流向")
    inflow, outflow = get_sector_fund_flow()
    if inflow:
        inflow_str = [f"{s['name']}({s['fund']:+.2f}亿)" for s in inflow]
        lines.append(f"  净流入: {' | '.join(inflow_str)}")
    if outflow:
        outflow_str = [f"{s['name']}({s['fund']:+.2f}亿)" for s in outflow]
        lines.append(f"  净流出: {' | '.join(outflow_str)}")
    if not inflow and not outflow:
        lines.append("  数据暂不可用")
    lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    print(format_sector_message())
