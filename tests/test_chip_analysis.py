"""筹码分布（成本分布）计算测试 —— 纯标准库，CI 不装 numpy/pandas 也能跑。

钉住的是「方向不能反」：上涨行情里平均成本必须低于现价、获利比例必须高；
下跌行情反过来。这类符号错误在白纸上完全看不出来，只有断言能抓。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.chip_analysis import (  # noqa: E402
    compute_chip_distribution,
    concentration_level,
    float_shares_from_mv,
    profit_level,
)

SHARES = 1e8          # 1 亿流通股
TURNOVER_VOL = 5e6    # 每天成交 500 万股 = 5% 换手


def bars(prices, volume=TURNOVER_VOL, spread=0.02):
    """按收盘价序列造日线：开收同价，上下各留 2% 影线。"""
    return [
        {"open": p, "high": p * (1 + spread), "low": p * (1 - spread),
         "close": p, "volume": volume}
        for p in prices
    ]


def trend(start, end, n=120):
    step = (end - start) / (n - 1)
    return [round(start + step * i, 2) for i in range(n)]


class ChipDistributionTest(unittest.TestCase):
    def test_uptrend_avg_cost_below_price_and_most_in_profit(self):
        prices = trend(10, 20)
        r = compute_chip_distribution(bars(prices), SHARES)
        self.assertTrue(r["available"], r.get("reason"))
        self.assertLess(r["avg_cost"], prices[-1], "上涨行情平均成本必须低于现价")
        self.assertGreater(r["profit_ratio"], 0.7, "上涨行情大部分筹码应浮盈")
        self.assertAlmostEqual(r["price"], prices[-1], places=2)

    def test_downtrend_avg_cost_above_price_and_mostly_trapped(self):
        prices = trend(20, 10)
        r = compute_chip_distribution(bars(prices), SHARES)
        self.assertGreater(r["avg_cost"], prices[-1], "下跌行情平均成本必须高于现价")
        self.assertLess(r["profit_ratio"], 0.3, "下跌行情大部分筹码应套牢")
        self.assertLess(r["deviation"], 0)

    def test_flat_price_is_very_concentrated(self):
        r = compute_chip_distribution(bars([10.0] * 120, spread=0.005), SHARES)
        self.assertLess(r["concentration"], 0.05, "价格不动时筹码应高度集中")
        self.assertAlmostEqual(r["peak_price"], 10.0, delta=0.1)
        self.assertEqual(r["concentration_level"], "高度集中")

    def test_90pct_range_brackets_peak(self):
        r = compute_chip_distribution(bars(trend(10, 20)), SHARES)
        self.assertLessEqual(r["cost_low"], r["peak_price"])
        self.assertGreaterEqual(r["cost_high"], r["peak_price"])
        self.assertLess(r["cost_low"], r["cost_high"])

    def test_high_turnover_forgets_old_chips(self):
        """90% 日换手意味着老筹码一天就被换光，平均成本应贴住最近几天的价格。"""
        prices = [10.0] * 100 + [20.0] * 5
        r = compute_chip_distribution(bars(prices, volume=9e7), SHARES)
        self.assertGreater(r["avg_cost"], 18.0, "老筹码应被高换手洗掉")

    def test_missing_float_shares_returns_unavailable(self):
        for bad in (None, 0, -1, "abc"):
            r = compute_chip_distribution(bars(trend(10, 12)), bad)
            self.assertFalse(r["available"], f"流通股本={bad!r} 时不能硬算")

    def test_too_few_bars_returns_unavailable(self):
        r = compute_chip_distribution(bars([10.0, 10.1, 10.2]), SHARES)
        self.assertFalse(r["available"])
        self.assertIn("K 线不足", r["reason"])

    def test_empty_hist_returns_unavailable(self):
        self.assertFalse(compute_chip_distribution([], SHARES)["available"])
        self.assertFalse(compute_chip_distribution(None, SHARES)["available"])


class ChipHelpersTest(unittest.TestCase):
    def test_float_shares_from_mv(self):
        self.assertAlmostEqual(float_shares_from_mv(2.78e10, 4.13), 2.78e10 / 4.13)
        self.assertIsNone(float_shares_from_mv(None, 4.13))
        self.assertIsNone(float_shares_from_mv(2.78e10, 0))

    def test_levels_direction(self):
        # 获利比例：越大越好
        self.assertEqual(profit_level(0.95), "几乎全部获利")
        self.assertEqual(profit_level(0.05), "几乎全部套牢")
        # 集中度：越小越集中
        self.assertEqual(concentration_level(0.05), "高度集中")
        self.assertEqual(concentration_level(0.60), "分散")

    def test_weights_are_normalized(self):
        r = compute_chip_distribution(bars(trend(10, 20)), SHARES)
        self.assertAlmostEqual(max(r["weights"]), 1.0, places=6, msg="最长的那根条应是 1.0")
        self.assertEqual(len(r["centers"]), len(r["weights"]))


if __name__ == "__main__":
    unittest.main()
