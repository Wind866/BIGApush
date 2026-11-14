"""K 线均线口径测试（纯静态解析，不导入 matplotlib，CI 不装绘图库也能跑）。

为什么单独钉这个：卡片上的均线是「看一眼就知道多空」的核心信息，改版时很容易被
顺手删成两条（最初就只有 MA5/MA20）。这里把「必须是 5/10/20/60 四条」写成断言，
外加一条「K 线根数要够画 MA60」——90 根时 MA60 前面会缺一大截，看着像断线。
"""
from __future__ import annotations

import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARD = os.path.join(ROOT, "utils", "stock_card.py")


def _module_const(name: str):
    """从源码里取模块级常量（不执行 import，避免拉 matplotlib）。"""
    with open(CARD, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=CARD)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"utils/stock_card.py 里找不到常量 {name}")


class StockCardMaTest(unittest.TestCase):
    def test_mas_are_5_10_20_60(self):
        windows = [int(w) for w, _ in _module_const("MAS")]
        self.assertEqual(windows, [5, 10, 20, 60], "均线必须是 5/10/20/60 日四条")

    def test_ma_colors_are_distinct(self):
        colors = [c for _, c in _module_const("MAS")]
        self.assertEqual(len(colors), len(set(colors)), "四条均线颜色不能重复，否则分不清")

    def test_kline_bars_cover_ma60(self):
        self.assertGreaterEqual(
            int(_module_const("KLINE_BARS")), 60,
            "K 线根数少于 60 时 MA60 画不出来",
        )

    def test_draw_kline_loops_over_mas(self):
        with open(CARD, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("for window, color in MAS", src, "_draw_kline 应按 MAS 循环画线")

    def test_legend_shows_latest_value(self):
        with open(CARD, encoding="utf-8") as fh:
            src = fh.read()
        # 图例写成「MA20 4.11」比只写「MA20」有用：当前均线值一眼可见
        self.assertIn('label=f"MA{window} {last:.2f}"', src)


if __name__ == "__main__":
    unittest.main()
