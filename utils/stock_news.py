#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""个股新闻抓取（做「舆情情绪 / 最新动态」的素材，多源降级）。

只取「标题 + 摘要 + 时间」，不做情感判断——那是 LLM 的活。任何源失败都返回
空列表，绝不阻断主分析：没有新闻时 LLM 仍可基于财务/资金数据归纳四段文字。

数据源（免费、无需 key）：
  1. 东方财富搜索（cmsArticleWebOld）—— JSONP，个股相关资讯
  2. 同花顺个股快讯兜底
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

BJ_TZ = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")
TIMEOUT = 12
MAX_ITEMS = 8


def _clean(html: str) -> str:
    """去 HTML 标签与实体，压成一行。"""
    if not html:
        return ""
    txt = re.sub(r"<[^>]+>", "", html)
    txt = (txt.replace("&nbsp;", " ").replace("&quot;", '"')
              .replace("&amp;", "&").replace("&#39;", "'"))
    return re.sub(r"\s+", " ", txt).strip()


def _fresh(title_time: str, max_days: int = 15) -> bool:
    """过旧的资讯不当「最新动态」。无法解析时间时保守放行。"""
    if not title_time:
        return True
    try:
        dt = datetime.strptime(title_time[:10], "%Y-%m-%d").replace(tzinfo=BJ_TZ)
    except (ValueError, TypeError):
        return True
    return dt >= datetime.now(BJ_TZ) - timedelta(days=max_days)


def _from_eastmoney(name: str, code: str) -> list:
    """东方财富全站搜索：按股票名检索个股资讯。"""
    param = {
        "uid": "", "keyword": name or code,
        "type": ["cmsArticleWebOld"], "client": "web",
        "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {
            "searchScope": "default", "sort": "time",
            "pageIndex": 1, "pageSize": MAX_ITEMS,
            "preTag": "", "postTag": "",
        }},
    }
    url = "https://search-api-web.eastmoney.com/search/jsonp"
    resp = requests.get(url, params={"cb": "cb", "param": json.dumps(param, ensure_ascii=False)},
                        headers={"User-Agent": UA, "Referer": "https://so.eastmoney.com/"},
                        timeout=TIMEOUT)
    resp.raise_for_status()
    text = resp.text.strip()
    m = re.search(r"cb\((.*)\)\s*;?$", text, re.S)
    if not m:
        return []
    data = json.loads(m.group(1))
    articles = (((data.get("result") or {}).get("cmsArticleWebOld")) or [])
    out = []
    for a in articles:
        title = _clean(a.get("title"))
        if not title:
            continue
        t = a.get("date") or a.get("time") or ""
        if not _fresh(t):
            continue
        content = _clean(a.get("content") or a.get("summary") or "")
        out.append({"title": title, "summary": content[:120], "time": t, "source": "东方财富"})
    return out[:MAX_ITEMS]


def _from_ths(name: str, code: str) -> list:
    """同花顺快讯兜底（关键词过滤个股相关条目）。"""
    url = "https://news.10jqka.com.cn/tapp/news/push/stock/"
    resp = requests.get(url, params={"page": 1, "tag": "", "track": "website"},
                        headers={"User-Agent": UA, "Referer": "https://news.10jqka.com.cn/"},
                        timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    items = (data.get("data") or []) if isinstance(data, dict) else []
    out = []
    for a in items:
        title = _clean(a.get("title") or "")
        if not title or (name and name not in title and code not in title):
            continue
        t = a.get("ctime") or a.get("time") or ""
        try:
            if t and isinstance(t, (int, float)):
                t = datetime.fromtimestamp(float(t), BJ_TZ).strftime("%Y-%m-%d")
        except (ValueError, OSError, TypeError):
            t = ""
        if not _fresh(t):
            continue
        out.append({"title": title, "summary": _clean(a.get("summary") or "")[:120],
                    "time": t, "source": "同花顺"})
    return out[:MAX_ITEMS]


def fetch_stock_news(name: str, code: str) -> list:
    """按 东方财富 → 同花顺 顺序降级；全失败返回 []。"""
    for fn in (_from_eastmoney, _from_ths):
        try:
            items = fn(name, code)
            if items:
                print(f"[NEWS] {code} 取到 {len(items)} 条（{items[0]['source']}）")
                return items
        except Exception as exc:  # noqa: BLE001  单源失败换下一个
            print(f"::warning::{code} 新闻源 {fn.__name__} 失败：{type(exc).__name__} {exc}")
    print(f"::warning::{code} 个股新闻全部不可用，LLM 将仅基于财务/资金数据归纳")
    return []
