#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""交易日判定的回归测试（标准库 unittest，无需第三方依赖、不联网）。

重点是「跨午夜补跑」这条路径：15:43 的收盘定稿档被调度延迟拖到次日凌晨时，
旧实现只看 `行情日期 == 今天`，一律判成非交易日静默退出，当天定稿永久丢失。
这里把时间与行情时间戳都注入成固定值，锁定行为。
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import intraday_selection as isel  # noqa: E402

BJ = timezone(timedelta(hours=8))


def _ts(y, m, d, hh=15, mm=0):
    return int(datetime(y, m, d, hh, mm, tzinfo=BJ).timestamp())


class TradingSessionTest(unittest.TestCase):
    def setUp(self):
        self._orig = isel.fetch_index_timestamp

    def tearDown(self):
        isel.fetch_index_timestamp = self._orig

    def _run(self, now, quote, allow_catchup=False):
        isel.fetch_index_timestamp = lambda: quote
        return isel.trading_session(now=now, allow_catchup=allow_catchup)

    def test_intraday_same_day_passes(self):
        # 2026-11-16 周一 13:13，行情时间戳是当天
        ok, _reason, data_date = self._run(
            datetime(2026, 11, 16, 13, 13, tzinfo=BJ), _ts(2026, 11, 16, 13, 10))
        self.assertTrue(ok)
        self.assertEqual(data_date, "2026-11-16")

    def test_intraday_after_midnight_is_skipped(self):
        # 19:13 兜底档拖到次日 03:00：盘中快照已无时效，必须跳过
        ok, reason, _ = self._run(
            datetime(2026, 11, 17, 3, 0, tzinfo=BJ), _ts(2026, 11, 16, 15, 0))
        self.assertFalse(ok)
        self.assertIn("非交易日", reason)

    def test_close_catchup_after_midnight_runs(self):
        # 15:43 收盘档拖到次日 01:26：按上一交易日补跑，数据日期=行情日期
        ok, reason, data_date = self._run(
            datetime(2026, 11, 17, 1, 26, tzinfo=BJ), _ts(2026, 11, 16, 15, 0),
            allow_catchup=True)
        self.assertTrue(ok)
        self.assertEqual(data_date, "2026-11-16")
        self.assertIn("跨夜补跑", reason)

    def test_close_catchup_on_weekend_runs(self):
        # 周五档拖到周六 02:00：周末也能补
        ok, _reason, data_date = self._run(
            datetime(2026, 11, 14, 2, 0, tzinfo=BJ), _ts(2026, 11, 13, 15, 0),
            allow_catchup=True)
        self.assertTrue(ok)
        self.assertEqual(data_date, "2026-11-13")

    def test_holiday_still_skipped_with_catchup(self):
        # 周三是节假日，行情停在周二 15:00：即使允许补跑也不能当成交易日跑
        ok, _reason, _ = self._run(
            datetime(2026, 11, 18, 15, 43, tzinfo=BJ), _ts(2026, 11, 17, 15, 0),
            allow_catchup=True)
        self.assertFalse(ok)

    def test_catchup_gap_is_bounded(self):
        # 行情停在 10 天前（长假/数据源故障）：不允许拿陈旧数据出「当日定稿」
        ok, _reason, _ = self._run(
            datetime(2026, 11, 16, 3, 0, tzinfo=BJ), _ts(2026, 11, 6, 15, 0),
            allow_catchup=True)
        self.assertFalse(ok)

    def test_calendar_outage_does_not_block_close(self):
        # 收盘时段行情接口全挂：按周末规则放行，数据日期取运行日
        def boom():
            raise RuntimeError("all sources down")
        isel.fetch_index_timestamp = boom
        ok, _reason, data_date = isel.trading_session(
            now=datetime(2026, 11, 16, 15, 43, tzinfo=BJ), allow_catchup=True)
        self.assertTrue(ok)
        self.assertEqual(data_date, "2026-11-16")

    def test_calendar_outage_blocks_pre_open(self):
        # 盘前连「现在是哪一天的数据」都判断不了，不能硬跑
        def boom():
            raise RuntimeError("all sources down")
        isel.fetch_index_timestamp = boom
        for allow in (False, True):
            with self.subTest(allow_catchup=allow):
                ok, _reason, _ = isel.trading_session(
                    now=datetime(2026, 11, 16, 3, 0, tzinfo=BJ), allow_catchup=allow)
                self.assertFalse(ok)


class GithubOutputTest(unittest.TestCase):
    def test_emits_data_date_and_catchup(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "out.txt")
        os.environ["GITHUB_OUTPUT"] = path
        try:
            isel.emit_github_output(True, "2026-11-13")
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        finally:
            os.environ.pop("GITHUB_OUTPUT", None)
        self.assertIn("is_trading=true", text)
        self.assertIn("data_date=2026-11-13", text)
        self.assertIn("catchup=true", text)


if __name__ == "__main__":
    unittest.main()
