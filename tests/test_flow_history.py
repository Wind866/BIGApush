# -*- coding: utf-8 -*-
"""近一个月资金流入流出（新浪资金流历史）的解析与接线测试。

用户要的是「最近一个月每个交易日资金流进流出多少」，这块有三个容易翻车的点：
  1. 接口返回的日期是倒序（asc=0），不排序直接画，柱子会从右往左倒着长；
  2. 数值是字符串型金额，``num()`` 转不出来时必须整条丢掉，不能塞 0；
  3. 拿不到数据要明确抛错，让卡片退回「不带资金流」的布局，而不是画一片空框。
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from utils import market_sources as s  # noqa: E402

SAMPLE = [
    {"opendate": "2026-11-13", "trade": "4.1300", "changeratio": "-0.0350467",
     "turnover": "550.709", "netamount": "-274758003.2400", "r0_net": "-159071043.9700"},
    {"opendate": "2026-11-12", "trade": "4.2800", "changeratio": "0.00469484",
     "turnover": "945.391", "netamount": "176137735.7900", "r0_net": "122526944.9100"},
    {"opendate": "2026-11-11", "trade": "4.2600", "changeratio": "-0.0138889",
     "turnover": "600.0", "netamount": "-251135441.5000", "r0_net": "-153140945.4900"},
]


class FlowHistoryParseTest(unittest.TestCase):
    def _fetch(self, payload, days=25):
        text = payload if isinstance(payload, str) else json.dumps(payload)
        with mock.patch.object(s, "http_text", lambda *a, **kw: text):
            return s.sina_fetch_flow_history("002131", days=days)

    def test_sorted_ascending(self):
        """接口给的是倒序，必须换成「左旧右新」，否则柱子反着长。"""
        rows = self._fetch(SAMPLE)
        self.assertEqual([r["date"] for r in rows],
                         ["2026-11-11", "2026-11-12", "2026-11-13"])

    def test_fields_parsed(self):
        rows = self._fetch(SAMPLE)
        last = rows[-1]
        self.assertEqual(last["date"], "2026-11-13")
        self.assertAlmostEqual(last["close"], 4.13, places=2)
        self.assertAlmostEqual(last["net"], -274758003.24, places=2)
        self.assertAlmostEqual(last["super_net"], -159071043.97, places=2)
        self.assertLess(last["pct"], 0)

    def test_bad_rows_dropped_not_zeroed(self):
        """金额转不出来 / 日期不合法 → 丢掉这条，绝不当 0 画一根平柱子。"""
        payload = SAMPLE + [
            {"opendate": "2026-11-10", "netamount": "-", "r0_net": "-"},
            {"opendate": "2026/11/09", "netamount": "1000"},
            {"opendate": "2026-11-07", "netamount": "123456"},
        ]
        rows = self._fetch(payload)
        dates = [r["date"] for r in rows]
        self.assertEqual(len(dates), 4, "三条正常 + 一条合法补录，共 4 条")
        self.assertIn("2026-11-07", dates, "合法补录要保留")
        self.assertNotIn("2026-11-10", [r["date"] for r in rows])
        self.assertNotIn("2026/11/09", [r["date"] for r in rows])

    def test_days_limit_keeps_latest(self):
        rows = self._fetch(SAMPLE, days=2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["date"], "2026-11-13", "截断必须保留最近的那几天")

    def test_empty_and_broken_raise(self):
        for payload in ([], "[]", "not-json", [{"opendate": "x"}]):
            with self.assertRaises(s.SourceFailure, msg=f"payload={payload!r}"):
                self._fetch(payload)

    def test_url_uses_market_prefix(self):
        seen = {}

        def fake(url, **kw):
            seen["url"] = url
            return json.dumps(SAMPLE)

        with mock.patch.object(s, "http_text", fake):
            s.sina_fetch_flow_history("600519", days=20)
        self.assertIn("daima=sh600519", seen["url"])
        self.assertIn("num=20", seen["url"])
        self.assertIn("MoneyFlow.ssl_qsfx_zjlrqs", seen["url"])


class CardWiringTest(unittest.TestCase):
    """卡片接的是「可选面板」：接口挂了必须能退回原布局，不能留空框。"""

    def test_card_has_flow_panel(self):
        card = os.path.join(ROOT, "utils", "stock_card.py")
        with open(card, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("flow_hist: Optional[list] = None", src)
        self.assertIn("_draw_flow(fig, flow_hist, flow_rect)", src)
        self.assertIn("FLOW_BARS = 20", src, "一个月按 20 个交易日画")
        self.assertIn("has_flow = has_chart and bool(flow_hist)", src,
                      "没有数据就不该占版面")

    def test_text_fallback_has_flow_line(self):
        query = os.path.join(ROOT, "stock_query.py")
        with open(query, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("近{len(rows)}日资金", src, "图片发不出去时文字里也要有")
        self.assertIn("新浪口径", src, "必须标口径，否则会和「主力净流入」混着看")


if __name__ == "__main__":
    unittest.main(verbosity=2)
