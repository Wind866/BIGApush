"""
市值口径统一工具

历史问题：同一个 `market_cap` 字段在不同模块里单位不一致（有的按元、有的按亿元），
而且取不到时会被填上 `close * 2e8` / 伪随机数这类假值，导致市值分档与筛选
建在虚构数据上。本模块把口径钉死：

    - 系统内部（数据库列、策略 DataFrame、接口入参）一律用「元」
    - 对外展示/写入选股结果时用 `to_yi()` 转「亿元」
    - 取不到就返回 None（展示为「—」），**绝不返回 0**，因为 0 会被当成真实的「市值 0 亿」
"""


def to_yuan(value):
    """把任意来源的市值规整为「元」；非法/缺失返回 None。"""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    # NaN 与 <=0 都视为未知（市值不可能是 0 或负数）
    if v != v or v <= 0:
        return None
    return v


def to_yi(value):
    """把「元」市值转成「亿元」（保留 2 位）；未知返回 None。"""
    v = to_yuan(value)
    return round(v / 1e8, 2) if v is not None else None


def from_row(row, key="market_cap"):
    """从 pandas Series / 普通 dict 里安全取市值（元），缺失或 NaN 返回 None。"""
    try:
        raw = row.get(key) if hasattr(row, "get") else row[key]
    except (KeyError, IndexError, TypeError):
        return None
    return to_yuan(raw)


def column_or_none(df, key="market_cap"):
    """取 DataFrame 里的市值列；列不存在返回 None（调用方按未知处理，不要造假值）。"""
    if df is None or key not in getattr(df, "columns", []):
        return None
    return df[key]
