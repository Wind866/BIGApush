#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""选股股票池的「数据新鲜度」守卫。

## 为什么需要它

选股主流程（`main.py::select_stocks`）为了性能，直接调
`calculate_indicators` / `select_stocks`，绕过了 `BaseStrategy.execute_selection`
里的 `_validate_data` 和停牌检查。`_validate_data` 里唯一的时效判断是
「最新数据距今不超过 5 年」，于是：

- 退市股（如 600001 邯郸钢铁，数据止于 2009-12-15）会被当成有效标的；
- 长期停牌股会用它停牌前最后一个交易日的 K 线算出「今天的信号」；
- 只要 `len(df) >= 60`，一切照旧，卡片上看不出任何异常。

这里用一个统一的口径把它们挡在策略之外：**全市场最新交易日**（由本批股票的
最新日期取众数得到）减去阈值，之前的一律算陈旧。阈值默认 3 个自然日，
足以容忍单日数据延迟，但挡得住停牌一周以上的票。

## 口径说明

- 一律以「数据里真实存在的最新日期」为准，不用 `datetime.now()`，
  避免非交易日 / 时区问题把整池误判成陈旧；
- 返回的 `stale` 明细用于日志与卡片告警，**不静默丢弃**。
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime
from typing import Any, Dict, Iterable, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MAX_LAG_DAYS",
    "latest_kline_date",
    "market_reference_date",
    "split_stale",
]

# 允许的最大滞后（自然日）。3 天≈跨一个周末，既能容忍数据源延迟一天，
# 也能挡住「停牌一周以上还在被推荐」。
DEFAULT_MAX_LAG_DAYS = 3


def latest_kline_date(df: Any) -> Optional[date]:
    """取单只股票 DataFrame 里最新的一根 K 线日期（df 是倒序，最新在前）。

    取不到或不合法一律返回 None，由调用方当作「无数据」处理。
    """
    if df is None:
        return None
    try:
        if getattr(df, "empty", True):
            return None
        raw = df.iloc[0]["date"]
    except Exception:  # noqa: BLE001 - 结构不对就当无数据
        return None

    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = str(raw).strip().split(" ")[0]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def market_reference_date(dates: Iterable[Optional[date]]) -> Optional[date]:
    """由一批股票的最新日期推断「全市场最新交易日」。

    取众数（出现次数最多的日期），而不是 max()：
    - max() 会被个别脏数据（未来日期、测试数据）带偏；
    - 众数在正常交易日几乎必然等于当天，且不受少数停牌股影响。
    众数并列时取较晚的那个。
    """
    known = [d for d in dates if d is not None]
    if not known:
        return None
    counter = Counter(known)
    top = max(counter.values())
    return max(d for d, cnt in counter.items() if cnt == top)


def split_stale(
    batch_data: Dict[str, Any],
    max_lag_days: int = DEFAULT_MAX_LAG_DAYS,
    reference: Optional[date] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """把股票池切成「新鲜」和「陈旧」两部分。

    参数：
        batch_data: {code: DataFrame}
        max_lag_days: 允许的最大滞后自然日
        reference: 指定的最新交易日；不传则从 batch_data 推断

    返回：
        (fresh, stats)
        fresh: {code: DataFrame}，可直接进入策略
        stats: {
            'reference': date|None,      # 判定基准日
            'total': int,                # 输入只数
            'fresh': int,                # 保留只数
            'stale': int,                # 剔除只数
            'no_date': int,              # 连日期都读不到的只数
            'samples': [(code, date_str)],  # 剔除样例，最多 10 条
            'oldest': date|None,         # 被剔除股票里最老的日期
        }
    """
    reference = reference or market_reference_date(
        latest_kline_date(df) for df in batch_data.values()
    )

    fresh: Dict[str, Any] = {}
    stats: Dict[str, Any] = {
        "reference": reference,
        "total": len(batch_data),
        "fresh": 0,
        "stale": 0,
        "no_date": 0,
        "future": 0,
        "samples": [],
        "oldest": None,
    }

    for code, df in batch_data.items():
        latest = latest_kline_date(df)
        if latest is None:
            stats["no_date"] += 1
            stats["stale"] += 1
            if len(stats["samples"]) < 10:
                stats["samples"].append((code, "无日期"))
            continue

        if reference is None:
            # 连基准日都推不出来（整批都是坏的），全部放行交给下游判断，
            # 但这条路径正常不会走到，出现即说明数据源整体异常。
            fresh[code] = df
            continue

        lag = (reference - latest).days
        if lag < 0:
            # K 线日期晚于全市场最新交易日 = 脏数据（时区错位、测试数据、
            # 未来日期）。不清掉的话它会被当成「最新」，把指标口径带偏。
            stats["future"] += 1
            stats["stale"] += 1
            if len(stats["samples"]) < 10:
                stats["samples"].append((code, f"{latest.isoformat()}(未来)"))
            continue

        if lag > max_lag_days:
            stats["stale"] += 1
            if stats["oldest"] is None or latest < stats["oldest"]:
                stats["oldest"] = latest
            if len(stats["samples"]) < 10:
                stats["samples"].append((code, latest.isoformat()))
            continue

        fresh[code] = df

    stats["fresh"] = len(fresh)
    if stats["stale"]:
        logger.warning(
            "股票池时效过滤：剔除 %d/%d 只（最新交易日 %s，阈值 %d 天）",
            stats["stale"], stats["total"],
            reference.isoformat() if reference else "未知", max_lag_days,
        )
    return fresh, stats
