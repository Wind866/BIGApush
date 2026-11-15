# -*- coding: utf-8 -*-
"""
交易日历回归测试

覆盖三件事：
1. 节假日判定与「前一交易日」推导——这是节后回看出错的地方；
2. 加载策略的降级顺序：内存 → 磁盘 → 网络 → 磁盘副本 → 排除周末；
3. 回退到周末规则时必须告警，且逐日回看不会刷新告警刷屏。

全部用假日历驱动，不联网、不依赖 akshare 是否装好。
"""

import logging
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

import utils.trade_date_utils as tdu

# 2026 年国庆假期：10-01 ~ 10-07 休市（10-03/04 为周末），10-08 恢复交易
# 尾部补到年末，避免用例里触发「日历已过期」告警污染输出
FAKE_CALENDAR = {
    date(2026, 9, 24),   # 周四
    date(2026, 9, 28),   # 周一
    date(2026, 9, 29),
    date(2026, 9, 30),
    date(2026, 10, 8),   # 国庆后首个交易日
    date(2026, 10, 9),
    date(2026, 10, 12),
    date(2026, 11, 16),
    date(2026, 12, 31),  # 覆盖到年末，covers_today 为真
}


class CalendarTestBase(unittest.TestCase):
    """每个用例独立临时目录 + 干净模块状态"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmpdir = tmp.name
        self.cache_path = os.path.join(self.tmpdir, "trade_calendar.csv")

        patches = [
            mock.patch.object(tdu, "_CACHE_PATH", self.cache_path),
            mock.patch.object(tdu, "_CACHE_TTL_SECONDS", 20 * 3600),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        self._reset_state()
        self.addCleanup(self._reset_state)

    @staticmethod
    def _reset_state():
        tdu._calendar = None
        tdu._calendar_min = None
        tdu._calendar_max = None
        tdu._calendar_source = "未加载"
        tdu._calendar_loaded_at = 0.0
        tdu._last_failure_at = 0.0
        tdu._warned_keys = set()

    def install_calendar(self, days=None, source="akshare"):
        """直接注入一份日历，绕过网络"""
        days = FAKE_CALENDAR if days is None else days
        tdu._calendar = set(days)
        tdu._calendar_min = min(days)
        tdu._calendar_max = max(days)
        tdu._calendar_source = source
        tdu._calendar_loaded_at = 0.0

    def write_cache(self, days=None, age_seconds=0.0):
        """写一份磁盘缓存，可指定「多久以前写的」"""
        days = FAKE_CALENDAR if days is None else days
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        with open(self.cache_path, "w", encoding="utf-8", newline="") as f:
            for d in sorted(days):
                f.write(d.strftime("%Y-%m-%d") + "\n")
        if age_seconds:
            old = datetime.now().timestamp() - age_seconds
            os.utime(self.cache_path, (old, old))


class TestHolidayJudgement(CalendarTestBase):
    """节假日口径：这是原先「排除周末」埋下的坑"""

    def setUp(self):
        super().setUp()
        self.install_calendar()

    def test_holiday_weekday_is_not_trading_day(self):
        """10-07 是周三但在国庆假期内，必须判为休市（周末规则会误判为交易日）"""
        self.assertEqual(date(2026, 10, 7).weekday(), 2)  # 确属周三
        self.assertFalse(tdu.is_trading_day("2026-10-07"))

    def test_first_day_after_holiday(self):
        self.assertTrue(tdu.is_trading_day("2026-10-08"))
        self.assertTrue(tdu.is_trading_day("20261008"))

    def test_weekend_still_not_trading(self):
        self.assertFalse(tdu.is_trading_day("2026-10-10"))  # 周六

    def test_previous_trading_day_skips_holiday(self):
        """国庆后第一天，前一交易日要跳过 7 天假期回到 09-30"""
        self.assertEqual(tdu.get_previous_trading_day("2026-10-08"), "2026-09-30")

    def test_previous_trading_day_normal(self):
        self.assertEqual(tdu.get_previous_trading_day("2026-10-12"), "2026-10-09")

    def test_trading_days_range(self):
        days = tdu.get_trading_days("2026-09-28", "2026-10-12")
        self.assertEqual(
            days,
            ["2026-09-28", "2026-09-29", "2026-09-30",
             "2026-10-08", "2026-10-09", "2026-10-12"],
        )

    def test_invalid_date_returns_false(self):
        self.assertFalse(tdu.is_trading_day("2026-13-45"))
        self.assertFalse(tdu.is_trading_day(""))


class TestLoadStrategy(CalendarTestBase):
    """加载顺序：内存 → 磁盘 → 网络 → 磁盘副本 → 排除周末"""

    def test_disk_cache_hit_skips_network(self):
        self.write_cache(age_seconds=60)
        with mock.patch.object(tdu, "_fetch_from_akshare") as fetch:
            self.assertFalse(tdu.is_trading_day("2026-10-07"))
        fetch.assert_not_called()
        self.assertEqual(tdu._calendar_source, "磁盘缓存")

    def test_network_fill_writes_cache(self):
        with mock.patch.object(tdu, "_fetch_from_akshare", return_value=set(FAKE_CALENDAR)):
            self.assertFalse(tdu.is_trading_day("2026-10-07"))
        self.assertEqual(tdu._calendar_source, "akshare")
        self.assertTrue(os.path.exists(self.cache_path))

    def test_stale_disk_cache_used_when_network_fails(self):
        self.write_cache(age_seconds=30 * 3600)  # 超过 20 小时 TTL
        with mock.patch.object(tdu, "_fetch_from_akshare", side_effect=OSError("network down")):
            with self.assertLogs("utils.trade_date_utils", level="WARNING"):
                self.assertFalse(tdu.is_trading_day("2026-10-07"))
        self.assertIn("已过期", tdu._calendar_source)

    def test_weekday_fallback_warns_loudly(self):
        """日历彻底拿不到时必须告警——静默回退正是原来的问题"""
        with mock.patch.object(tdu, "_fetch_from_akshare", side_effect=ImportError("no akshare")):
            with self.assertLogs("utils.trade_date_utils", level="WARNING") as captured:
                # 10-07 是周三，周末规则会误判为交易日；10-11 是周日
                self.assertTrue(tdu.is_trading_day("2026-10-07"))
                self.assertFalse(tdu.is_trading_day("2026-10-11"))
                self.assertIsNone(tdu._calendar)
                self.assertEqual(tdu._calendar_source, "回退:排除周末")
        self.assertIn("排除周末", "\n".join(captured.output))
        self.assertIn("节假日会被误判", "\n".join(captured.output))

    def test_failure_backoff_avoids_retry_storm(self):
        """一次网络故障不能被逐日回看放大成几十次请求"""
        with mock.patch.object(
            tdu, "_fetch_from_akshare", side_effect=OSError("network down")
        ) as fetch:
            tdu.is_trading_day("2026-10-07")
            for _ in range(20):
                tdu.is_trading_day("2026-10-07")
        self.assertEqual(fetch.call_count, 1)

    def test_force_refresh_bypasses_backoff(self):
        with mock.patch.object(
            tdu, "_fetch_from_akshare", side_effect=OSError("network down")
        ) as fetch:
            tdu._load_calendar()
            tdu._load_calendar(force_refresh=True)
        self.assertEqual(fetch.call_count, 2)

    def test_warning_emitted_once(self):
        """逐日回看会反复触发回退，不能每次都刷日志"""
        with mock.patch.object(tdu, "_fetch_from_akshare", side_effect=ImportError("no akshare")):
            with self.assertLogs("utils.trade_date_utils", level="WARNING") as captured:
                for _ in range(10):
                    tdu.is_trading_day("2026-10-07")
        self.assertEqual(len(captured.output), 1, captured.output)

    def test_out_of_range_falls_back_with_warning(self):
        self.install_calendar()
        with self.assertLogs("utils.trade_date_utils", level="WARNING") as captured:
            # 2027-01-05 超出日历覆盖范围，按工作日判定
            self.assertTrue(tdu.is_trading_day("2027-01-05"))
            # 2027-01-02 是周六
            self.assertFalse(tdu.is_trading_day("2027-01-02"))
        self.assertIn("超出交易日历覆盖范围", "\n".join(captured.output))

    def test_parse_akshare_dataframe(self):
        """不联网验证解析：akshare 的 trade_date 列是 datetime.date 对象"""
        class FakeFrame:
            empty = False
            columns = ["trade_date"]

            def __init__(self):
                self._values = [date(2026, 10, 8), date(2026, 10, 9)]

            def __getitem__(self, key):
                return self

            def tolist(self):
                return list(self._values)

        fake_ak = mock.MagicMock()
        fake_ak.tool_trade_date_hist_sina.return_value = FakeFrame()
        with mock.patch.dict("sys.modules", {"akshare": fake_ak}):
            days = tdu._fetch_from_akshare()
        self.assertEqual(days, {date(2026, 10, 8), date(2026, 10, 9)})

    def test_parse_akshare_empty_raises(self):
        class EmptyFrame:
            empty = True
            columns = ["trade_date"]

        fake_ak = mock.MagicMock()
        fake_ak.tool_trade_date_hist_sina.return_value = EmptyFrame()
        with mock.patch.dict("sys.modules", {"akshare": fake_ak}):
            with self.assertRaises(RuntimeError):
                tdu._fetch_from_akshare()


class TestLookbackGuard(unittest.TestCase):
    """回看上限：is_trading_day 恒为 False 时不能死循环"""

    def test_bounded_lookback(self):
        with mock.patch.object(tdu, "is_trading_day", return_value=False) as fake:
            with self.assertLogs("utils.trade_date_utils", level="ERROR"):
                result = tdu.get_previous_trading_day("2026-11-16")
        self.assertEqual(result, "2026-11-16")
        self.assertEqual(fake.call_count, tdu._MAX_LOOKBACK_DAYS)


if __name__ == "__main__":
    unittest.main()
