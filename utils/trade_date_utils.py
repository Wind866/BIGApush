# -*- coding: utf-8 -*-
"""
交易日工具模块

提供判断日期是否为交易日的功能。

交易日历口径
------------
以 akshare 的新浪交易日历快照（``tool_trade_date_hist_sina``）为准：一次拉取即覆盖
1990 年至当年年末约 8800 个交易日，含法定节假日与调休，因此节后第一天的回看、
「上一交易日」推导都不会把假期里的工作日误当成交易日。

加载策略（逐级降级，每一级都有日志，不静默）
--------------------------------------------
1. 进程内缓存：整份日历只拉一次，``get_previous_trading_day`` 这类逐日回看的调用
   不会产生额外请求。
2. 磁盘缓存：``data/akshare_cache/trade_calendar.csv``，20 小时内直接复用，既省请求，
   也让同一 runner 上的多个 python 步骤（预检 / 主流程）共享一份日历。
3. 网络拉取：akshare 不可用或超时时，退回上一份磁盘副本（即使过期也比没有强）。
4. 最后兜底：日历完全拿不到时，回退为「排除周六周日」，并 **WARNING 告警**——
   该模式下法定节假日会被误判为交易日。

历史背景：本模块原先优先调用 Tushare ``trade_cal``，但 ``tushare`` 从未进入
``requirements.txt``、``config/tushare_config.json`` 也不在仓库里，CI 上该分支
是不可达的死代码，实际一直走「排除周末」。这就是节后回看会出错的根因。
"""

import csv
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Set

# 配置日志记录器
logger = logging.getLogger(__name__)

# 磁盘缓存：沿用 data/akshare_cache/ 约定（该目录已在 .gitignore 中）
_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "akshare_cache", "trade_calendar.csv",
)
_CACHE_TTL_SECONDS = 20 * 3600

# 向后回看的上限，防止日历异常时空转（正常最长假期也不超过 10 天）
_MAX_LOOKBACK_DAYS = 60

# 拉取失败后的退避时间：避免 get_previous_trading_day 逐日回看时反复打网络
_FAILURE_BACKOFF_SECONDS = 300

_lock = threading.RLock()
_calendar: Optional[Set[date]] = None
_calendar_min: Optional[date] = None
_calendar_max: Optional[date] = None
_calendar_source: str = "未加载"
_calendar_loaded_at: float = 0.0
_last_failure_at: float = 0.0
_warned_keys: Set[str] = set()


def _warn_once(key: str, msg: str, *args) -> None:
    """同一条告警每个进程只打一次，避免逐日回看时刷屏"""
    if key in _warned_keys:
        return
    _warned_keys.add(key)
    logger.warning(msg, *args)


def _parse_date(date_str) -> date:
    """解析 YYYY-MM-DD / YYYYMMDD / date / datetime，失败抛 ValueError"""
    if isinstance(date_str, datetime):
        return date_str.date()
    if isinstance(date_str, date):
        return date_str
    text = str(date_str).strip()
    fmt = "%Y-%m-%d" if "-" in text else "%Y%m%d"
    return datetime.strptime(text, fmt).date()


def _read_cache_file() -> Optional[Set[date]]:
    """读磁盘缓存；任何异常都当作没有缓存，不影响主流程"""
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8", newline="") as f:
            days = set()
            for row in csv.reader(f):
                if not row or not row[0].strip():
                    continue
                try:
                    days.add(_parse_date(row[0]))
                except ValueError:
                    continue
        return days or None
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.debug("交易日历磁盘缓存读取失败: %s", e)
        return None


def _write_cache_file(days: Set[date]) -> None:
    """原子写磁盘缓存；写不了就跳过，不因为缓存失败拖垮主流程"""
    tmp = _CACHE_PATH + ".tmp"
    try:
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            for d in sorted(days):
                writer.writerow([d.strftime("%Y-%m-%d")])
        os.replace(tmp, _CACHE_PATH)
    except OSError as e:
        logger.debug("交易日历磁盘缓存写入失败: %s", e)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _fetch_from_akshare() -> Set[date]:
    """从 akshare 拉取新浪交易日历，返回交易日集合"""
    import akshare as ak

    df = ak.tool_trade_date_hist_sina()
    if df is None or getattr(df, "empty", True):
        raise RuntimeError("akshare 返回空的交易日历")

    column = "trade_date" if "trade_date" in df.columns else df.columns[0]
    days: Set[date] = set()
    for value in df[column].tolist():
        try:
            days.add(_parse_date(value))
        except (ValueError, TypeError):
            continue
    if not days:
        raise RuntimeError("akshare 交易日历解析后为空")
    return days


def _load_calendar(force_refresh: bool = False) -> Optional[Set[date]]:
    """
    加载交易日历（进程内单例）

    Returns:
        Set[date] 交易日集合；彻底拿不到时返回 None，调用方需回退
    """
    global _calendar, _calendar_min, _calendar_max, _calendar_source, _calendar_loaded_at
    global _last_failure_at

    with _lock:
        if _calendar is not None and not force_refresh:
            return _calendar

        # 上次拉取已经失败：退避期内直接返回，不再打网络。
        # 否则 get_previous_trading_day 逐日回看会把一次网络故障放大成几十次请求
        if (
            not force_refresh
            and _calendar is None
            and _last_failure_at
            and time.time() - _last_failure_at < _FAILURE_BACKOFF_SECONDS
        ):
            logger.debug("交易日历处于失败退避期，本次按回退规则判断")
            return None

        cached = _read_cache_file()
        cache_age = None
        if cached is not None:
            try:
                cache_age = time.time() - os.path.getmtime(_CACHE_PATH)
            except OSError:
                cache_age = None

        # 1) 磁盘缓存仍在有效期内：直接复用，不打网络
        if cached is not None and cache_age is not None and cache_age < _CACHE_TTL_SECONDS:
            days, source = cached, "磁盘缓存"
        else:
            # 2) 拉网络；失败则退回磁盘副本，最后才回退到周末规则
            try:
                days = _fetch_from_akshare()
                _write_cache_file(days)
                source = "akshare"
            except Exception as e:
                if cached is not None:
                    days, source = cached, "磁盘缓存(已过期)"
                    logger.warning(
                        "akshare 拉取交易日历失败（%s: %s），改用 %.1f 小时前的磁盘副本；"
                        "若跨年或长假，请检查该副本是否已过期",
                        type(e).__name__, e,
                        (cache_age or 0) / 3600,
                    )
                else:
                    _warn_once(
                        "no_calendar",
                        "交易日历不可用（%s: %s），回退为「排除周末」判断——"
                        "该模式下法定节假日会被误判为交易日，节后回看与「上一交易日」会算错，"
                        "请检查网络或 akshare 是否正常",
                        type(e).__name__, e,
                    )
                    _calendar, _calendar_min, _calendar_max = None, None, None
                    _calendar_source, _calendar_loaded_at = "回退:排除周末", time.time()
                    _last_failure_at = time.time()
                    return None

        _calendar = days
        _calendar_min = min(days)
        _calendar_max = max(days)
        _calendar_source = source
        _calendar_loaded_at = time.time()
        _last_failure_at = 0.0

        logger.debug(
            "交易日历已加载：%s ~ %s，共 %d 个交易日（来源：%s）",
            _calendar_min, _calendar_max, len(days), source,
        )

        # 日历过期是「静默出错」的经典场景：跨年后新浪文件没更新，超出部分只能按周末猜
        today = date.today()
        if _calendar_max < today:
            _warn_once(
                "stale_calendar",
                "交易日历只覆盖到 %s，已过期（今天 %s），超出范围的日期将按「排除周末」判断，"
                "请确认 akshare 能否取到新一年的日历",
                _calendar_max, today,
            )
        return _calendar


def _weekday_fallback(d: date) -> bool:
    """兜底规则：周一至周五算交易日（不含法定节假日判断）"""
    return d.weekday() < 5


def calendar_status() -> Dict[str, object]:
    """
    返回交易日历的加载状态，供自检脚本 / CI 断言使用

    典型用法：``python -m utils.trade_date_utils``
    """
    calendar = _load_calendar()
    today = date.today()
    return {
        "loaded": calendar is not None,
        "source": _calendar_source,
        "min_date": _calendar_min.isoformat() if _calendar_min else None,
        "max_date": _calendar_max.isoformat() if _calendar_max else None,
        "trading_days": len(calendar) if calendar else 0,
        "covers_today": bool(_calendar_min and _calendar_max and _calendar_min <= today <= _calendar_max),
        "disk_cache": _CACHE_PATH,
        "loaded_at": (
            datetime.fromtimestamp(_calendar_loaded_at).isoformat(timespec="seconds")
            if _calendar_loaded_at else None
        ),
    }


def is_trading_day(date_str) -> bool:
    """
    判断指定日期是否为交易日

    以 akshare 交易日历为准（含法定节假日与调休）；日历覆盖范围之外、
    或日历完全不可用时，回退为「排除周六周日」并告警。

    参数:
        date_str: 日期字符串，支持 YYYY-MM-DD 或 YYYYMMDD 格式
    返回:
        bool: 是否为交易日
    """
    try:
        d = _parse_date(date_str)
    except (ValueError, TypeError) as e:
        logger.error("判断交易日时日期解析失败: %s（%s）", date_str, e)
        return False

    try:
        calendar = _load_calendar()
        if calendar is not None:
            if _calendar_min <= d <= _calendar_max:
                result = d in calendar
                logger.debug("%s %s交易日（akshare 日历）", d, "是" if result else "不是")
                return result
            _warn_once(
                "out_of_range",
                "日期 %s 超出交易日历覆盖范围（%s ~ %s），回退为「排除周末」判断",
                d, _calendar_min, _calendar_max,
            )
        result = _weekday_fallback(d)
        logger.debug("%s %s交易日（回退：排除周末）", d, "是" if result else "不是")
        return result
    except Exception as e:
        logger.error("判断交易日时出错: %s", e)
        return False


def get_trading_days(start_date, end_date) -> List[str]:
    """
    获取指定日期范围内的交易日列表

    直接对整份日历做区间过滤，不逐日请求接口。

    参数:
        start_date: 开始日期，支持 YYYY-MM-DD 或 YYYYMMDD 格式
        end_date: 结束日期，支持 YYYY-MM-DD 或 YYYYMMDD 格式
    返回:
        List[str]: 交易日列表，格式为 YYYY-MM-DD
    """
    try:
        start = _parse_date(start_date)
        end = _parse_date(end_date)
    except (ValueError, TypeError) as e:
        logger.error("获取交易日列表时日期解析失败: %s ~ %s（%s）", start_date, end_date, e)
        return []

    if end < start:
        logger.warning("结束日期 %s 早于开始日期 %s，返回空列表", end, start)
        return []

    try:
        calendar = _load_calendar()
        out_of_range = 0
        trading_days: List[str] = []
        current = start
        while current <= end:
            if calendar is not None and _calendar_min <= current <= _calendar_max:
                hit = current in calendar
            else:
                hit = _weekday_fallback(current)
                if calendar is not None:
                    out_of_range += 1
            if hit:
                trading_days.append(current.strftime("%Y-%m-%d"))
            current += timedelta(days=1)

        if out_of_range:
            _warn_once(
                "range_partial",
                "区间 %s ~ %s 有 %d 天超出交易日历覆盖范围，这部分按「排除周末」估算",
                start, end, out_of_range,
            )
        logger.info("获取到 %d 个交易日", len(trading_days))
        return trading_days
    except Exception as e:
        logger.error("获取交易日列表时出错: %s", e)
        return []


def get_trading_days_between(start_date, end_date) -> int:
    """
    计算两个日期之间的交易日天数

    参数:
        start_date: 开始日期，支持 YYYY-MM-DD 格式或 date 对象
        end_date: 结束日期，支持 YYYY-MM-DD 格式或 date 对象
    返回:
        int: 交易日天数
    """
    try:
        # 获取交易日列表并返回长度
        trading_days = get_trading_days(start_date, end_date)
        return len(trading_days) - 1  # 减去1，因为不包括买入当天
    except Exception as e:
        logger.error("计算交易日天数时出错: %s", e)
        return 0


def get_previous_trading_day(date_str) -> str:
    """
    获取指定日期的前一个交易日

    参数:
        date_str: 日期字符串，支持 YYYY-MM-DD 或 YYYYMMDD 格式
    返回:
        str: 前一个交易日，格式为 YYYY-MM-DD；查找失败时原样返回入参
    """
    try:
        d = _parse_date(date_str)
    except (ValueError, TypeError) as e:
        logger.error("获取前一个交易日时日期解析失败: %s（%s）", date_str, e)
        return date_str

    # 上限保护：日历异常时 is_trading_day 可能恒为 False，无上限会死循环
    current = d - timedelta(days=1)
    for _ in range(_MAX_LOOKBACK_DAYS):
        if is_trading_day(current):
            previous = current.strftime("%Y-%m-%d")
            logger.debug("%s 的前一个交易日是 %s", date_str, previous)
            return previous
        current -= timedelta(days=1)

    logger.error(
        "向前查找 %d 天仍未找到 %s 的交易日，请检查交易日历（当前来源：%s）",
        _MAX_LOOKBACK_DAYS, date_str, _calendar_source,
    )
    return date_str


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print(json.dumps(calendar_status(), ensure_ascii=False, indent=2, default=str))
