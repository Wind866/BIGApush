# -*- coding: utf-8 -*-
"""东财通道探针（熔断）回归测试。

背景：东财 push2 对部分出口 IP 是「直接 RST」而不是「超时」。原来的顺序是
先把 4 个域名各撞一遍（每个 8 秒 × 2 次重试），才肯换新浪 —— 每天开盘前白等
几十秒，而且日志里看不出是「被拦」还是「接口挂了」。

现在先打一个 2 秒的探针：判死就整轮跳过东财，只保留「东财能通就用全口径」。
这组用例钉住三件事：
  1. 探针判死 → 一次都不请求东财 clist / ulist.np（不产生二次等待）；
  2. 探针判活 → 仍然以东财为主源，行为与本轮改动前一致（不能白丢全口径）；
  3. 探针结论在进程内缓存，一次运行只探一次。
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from utils import biga_sources as bs  # noqa: E402


def _row(code: str, pct: float = 5.0, fund: bool = False) -> dict:
    row = bs._row(code, f"股票{code}", bs._now_ts())
    row.update({"f2": 10.0, "f3": pct, "f6": 1e8, "f8": 3.0, "f21": 5e9})
    if fund:
        row.update({"f62": 1.2e7, "f184": 8.0, "f66": 5e6, "f69": 3.0})
    return row


class SnapshotProbeTest(unittest.TestCase):
    def setUp(self):
        bs.reset_probe()
        self.addCleanup(bs.reset_probe)

    def test_probe_dead_skips_eastmoney_entirely(self):
        """探针判死：不碰 clist、不碰 ulist.np，直接新浪，且原文记下熔断原因。"""
        em_calls, upgrade_calls, sina_calls = [], [], []

        def fake_em(*a, **kw):
            em_calls.append(1)
            raise AssertionError("熔断后不该再请求东财榜单")

        def fake_upgrade(rows):
            upgrade_calls.append(1)
            raise AssertionError("熔断后不该再请求东财 ulist.np")

        def fake_sina(*a, **kw):
            sina_calls.append(1)
            return [_row("000001"), _row("600000")], bs._now_ts()

        with mock.patch.object(bs, "em_probe", return_value=(False, "RemoteDisconnected")), \
             mock.patch.object(bs, "em_strong_stocks", fake_em), \
             mock.patch.object(bs, "em_upgrade", fake_upgrade), \
             mock.patch.object(bs, "sina_strong_stocks", fake_sina), \
             mock.patch.object(bs, "tencent_patch", lambda rows: 0), \
             mock.patch.object(bs, "sina_flow_patch", lambda rows: 0):
            snap = bs.fetch_snapshot(scan_floor=2.0, max_pages=2)

        self.assertEqual(em_calls, [], "东财榜单不该被调用")
        self.assertEqual(upgrade_calls, [], "东财 ulist.np 不该被调用")
        self.assertEqual(len(sina_calls), 1, "应由新浪接手")
        self.assertEqual(snap.source, "sina")
        self.assertFalse(snap.fund_comparable, "备用口径分数不可与东财口径比较")
        joined = "；".join(snap.notes)
        self.assertIn("熔断", joined, "降级轨迹里必须写清是东财被拦")
        self.assertIn("RemoteDisconnected", joined, "要留住原始异常，便于事后定性")

    def test_probe_alive_keeps_eastmoney_as_primary(self):
        """探针判活：仍以东财为主源，全口径评分不受这次改动影响。"""
        sina_calls = []
        with mock.patch.object(bs, "em_probe", return_value=(True, "指数报价正常")), \
             mock.patch.object(bs, "em_strong_stocks",
                               lambda *a, **kw: ([_row("000001", fund=True)], 1700000000)), \
             mock.patch.object(bs, "sina_strong_stocks",
                               lambda *a, **kw: sina_calls.append(1) or []):
            snap = bs.fetch_snapshot(scan_floor=2.0, max_pages=1)

        self.assertEqual(snap.source, "eastmoney")
        self.assertEqual(snap.quality, "full")
        self.assertTrue(snap.fund_comparable)
        self.assertEqual(sina_calls, [], "东财可用时不该多打新浪")
        self.assertTrue(snap.has_fund)

    def test_probe_cached_between_calls(self):
        """一次运行只探一次：5 分钟内的重复调用必须吃缓存。"""
        calls = []

        def fake_json(url, **kw):
            calls.append(url)
            return {"data": {"f43": 413}}

        with mock.patch.object(bs.s, "http_json", fake_json):
            self.assertTrue(bs.em_probe(timeout=1)[0])
            self.assertTrue(bs.em_probe(timeout=1)[0])
            self.assertTrue(bs.em_probe(timeout=1)[0])
        self.assertEqual(len(calls), 1, "缓存失效了：每次都在真发请求")

    def test_probe_can_be_disabled(self):
        """eastmoney_probe=false 时退回旧行为：不做探针，直接试东财。"""
        bs.CFG["eastmoney_probe"] = False
        self.addCleanup(lambda: bs.configure({}))

        def boom(*a, **kw):
            raise AssertionError("开关关闭时不该探测")

        with mock.patch.object(bs.s, "http_json", boom):
            ok, detail = bs.em_probe(timeout=1)
        self.assertTrue(ok)
        self.assertIn("探针已关闭", detail)


class IndexTimestampProbeTest(unittest.TestCase):
    def setUp(self):
        bs.reset_probe()
        self.addCleanup(bs.reset_probe)

    def test_dead_probe_falls_back_to_tencent_directly(self):
        """东财判死时，交易日判断直接取腾讯，不再轮 4 个域名。"""
        fields = ["0"] * 32
        fields[30] = "20261114150000"
        gtimg = "~".join(fields)
        em_hits = []

        def fake_json(url, **kw):
            em_hits.append(url)
            raise AssertionError("熔断后不该再请求东财指数接口")

        with mock.patch.object(bs, "em_probe", return_value=(False, "RemoteDisconnected")), \
             mock.patch.object(bs.s, "http_json", fake_json), \
             mock.patch.object(bs.s, "http_text", lambda *a, **kw: gtimg):
            ts, label = bs.fetch_index_timestamp()

        self.assertEqual(em_hits, [])
        self.assertIn("腾讯", label)
        self.assertGreater(ts, 1_000_000_000)


class LimitUpCrosscheckTest(unittest.TestCase):
    """降级口径的涨停家数要拿东财涨停池核一遍（push2ex 常在被拦时仍可用）。"""

    def setUp(self):
        sys.path.insert(0, ROOT)
        import intraday_selection
        self.m = intraday_selection

    def test_consistent_count_is_reported_as_consistent(self):
        overview = {"limit_up": 37, "notes": ["候选池来源：新浪"]}
        with mock.patch.object(bs, "em_limit_up_count", return_value=(37, "2026-11-13")):
            self.m.crosscheck_limit_up(overview)
        tail = overview["notes"][-1]
        self.assertIn("东财涨停池 37 家", tail)
        self.assertIn("一致", tail)
        self.assertIn("2026-11-13", tail, "必须带数据日期，否则分不清核的是哪天的")

    def test_large_gap_is_called_out(self):
        overview = {"limit_up": 10, "notes": []}
        with mock.patch.object(bs, "em_limit_up_count", return_value=(40, "2026-11-13")):
            self.m.crosscheck_limit_up(overview)
        self.assertIn("差异", overview["notes"][-1])

    def test_failure_does_not_break_push(self):
        overview = {"limit_up": 37, "notes": []}
        with mock.patch.object(bs, "em_limit_up_count", side_effect=bs.s.SourceFailure("挂了")):
            self.m.crosscheck_limit_up(overview)
        self.assertEqual(overview["notes"], [], "核不上就不写，绝不能因此中断推送")


if __name__ == "__main__":
    unittest.main(verbosity=2)
