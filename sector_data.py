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
        print(f"⚠️ {kind} 板块全部数据源不可用：{type(exc).__name__} {exc}")
        return []


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


def get_concept_sectors():
    """获取概念板块涨跌"""
    try:
        url = "https://vip.stock.finance.sina.com.cn/q/view/newSinaGN.php"
        resp = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        resp.encoding = "gbk"  # 新浪板块接口返回 GBK，不指定会乱码
        match = re.search(r"=\s*(\{.*\})", resp.text, re.DOTALL)
        if match:
            data = json.loads(match.group(1))
            sectors = []
            for v in data.values():
                parts = v.split(",")
                if len(parts) > 4:
                    name = parts[1]
                    pct = float(parts[3]) if parts[3] else 0
                    sectors.append({"name": name, "pct": pct})
            sectors.sort(key=lambda x: x["pct"], reverse=True)
            if sectors:
                return sectors
    except Exception:
        pass
    return _board_fallback("concept")


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
