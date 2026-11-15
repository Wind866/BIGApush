"""新闻 / 盘中推送的「静默漏推」回归测试。

这三条是本次审出的 P0，都不是崩溃型 bug，而是「流程走完、运行页绿色、
群里却什么都没收到」：

1. 三个数据源全挂时仍推一条 130 字空壳消息，并写台账标记本档期已推送；
2. 未配置 webhook 时 push() 返回 True，同样把「没发」记成「已推送」；
3. 标题用运行日期，而 GitHub cron 实测延迟 4~8 小时且跨日 —— 日期错行。

只要台账被脏记录占住，后续所有兜底 cron 都会被幂等守卫跳过，当天彻底漏推。
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import daily_news  # noqa: E402
import evening_news  # noqa: E402
import intraday_selection  # noqa: E402

EMPTY_SECTOR = "💡 概念板块\n  ⚠️ 数据源暂不可用\n💰 板块资金流向\n  数据暂不可用"
REAL_SECTOR = "📊 行业板块\n  🔴 领涨: 银行(+1.20%)\n  🟢 领跌: 传媒(-0.80%)"


class TestContentGate(unittest.TestCase):
    def test_sector_placeholder_is_not_real_data(self):
        self.assertFalse(daily_news.sector_has_data(EMPTY_SECTOR))
        self.assertFalse(evening_news.sector_has_data(""))
        self.assertTrue(daily_news.sector_has_data(REAL_SECTOR))

    def test_all_sources_down_rejected(self):
        for mod in (daily_news, evening_news):
            ok, blocks = mod.content_quality([], EMPTY_SECTOR, [])
            self.assertFalse(ok, mod.__name__)
            self.assertEqual(sum(blocks.values()), 0)

    def test_one_block_still_rejected(self):
        ok, _ = daily_news.content_quality([], EMPTY_SECTOR, ["要闻1", "要闻2"])
        self.assertFalse(ok)

    def test_two_blocks_accepted(self):
        ok, blocks = daily_news.content_quality(["上证指数 3200"], EMPTY_SECTOR,
                                                ["要闻1", "要闻2"])
        self.assertTrue(ok)
        self.assertEqual(sum(blocks.values()), 2)


class TestEmptyPayloadNeverMarksPushed(unittest.TestCase):
    """P0：空壳消息既不能推送，更不能写台账。"""

    def _run(self, mod, patched_send):
        env = {"FEISHU_WEBHOOK": "https://example.invalid/hook"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(mod, "get_index_data", return_value=[]), \
             mock.patch.object(mod, "get_news", return_value=[]), \
             mock.patch.object(mod, "format_sector_message", return_value=EMPTY_SECTOR), \
             mock.patch.object(mod, "should_skip", return_value=False), \
             mock.patch.object(mod, "send_feishu") as send, \
             mock.patch.object(mod, "mark_pushed") as mark:
            patched_send(send)
            with self.assertRaises(SystemExit) as ctx:
                mod.main()
            return ctx.exception.code, send, mark

    def test_daily_news_all_sources_down(self):
        code, send, mark = self._run(daily_news, lambda s: None)
        self.assertEqual(code, 1)
        send.assert_not_called()
        mark.assert_not_called()

    def test_evening_news_all_sources_down(self):
        code, send, mark = self._run(evening_news, lambda s: None)
        self.assertEqual(code, 1)
        send.assert_not_called()
        mark.assert_not_called()

    def test_force_allows_emergency_push(self):
        """FORCE_PUSH=1 是应急通道，仍应允许推送空壳（人工已知情）。"""
        env = {"FEISHU_WEBHOOK": "https://example.invalid/hook", "FORCE_PUSH": "1"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(daily_news, "get_index_data", return_value=[]), \
             mock.patch.object(daily_news, "get_news", return_value=[]), \
             mock.patch.object(daily_news, "format_sector_message", return_value=EMPTY_SECTOR), \
             mock.patch.object(daily_news, "should_skip", return_value=False), \
             mock.patch.object(daily_news, "send_feishu", return_value=True) as send, \
             mock.patch.object(daily_news, "mark_pushed") as mark:
            daily_news.main()
        send.assert_called_once()
        mark.assert_called_once()


class TestNoFalseSuccess(unittest.TestCase):
    def test_news_senders_return_false_without_webhook(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(daily_news.send_feishu("hello"))
            self.assertFalse(evening_news.send_feishu("hello"))

    def test_intraday_push_returns_false_without_webhook(self):
        """旧实现返回 True → main() 紧接着 mark_pushed() 污染台账。"""
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(intraday_selection.push({}, "fallback text", ""))

    def test_intraday_push_checks_business_code(self):
        card = {"msg_type": "interactive", "card": {}}
        with mock.patch.dict(os.environ, {"FEISHU_WEBHOOK": "https://x"}, clear=True), \
             mock.patch.object(intraday_selection.fw, "send_payload",
                               return_value={"code": 19021}) as card_send, \
             mock.patch.object(intraday_selection.fw, "send_text",
                               return_value={"code": 0}):
            self.assertTrue(intraday_selection.push(card, "fallback", "https://x"))
        card_send.assert_called_once()


class TestDateWording(unittest.TestCase):
    """cron 延迟到下午 / 次日时，标题不能把运行日当成档期日。"""

    def _message(self, mod):
        env = {"FEISHU_WEBHOOK": "https://example.invalid/hook", "EXPECT_HHMM": "0843"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(mod, "get_index_data", return_value=["上证指数 3200.00 +0.50%"]), \
             mock.patch.object(mod, "get_news", return_value=["• 要闻一"]), \
             mock.patch.object(mod, "format_sector_message", return_value=REAL_SECTOR), \
             mock.patch.object(mod, "should_skip", return_value=False), \
             mock.patch.object(mod, "send_feishu", return_value=True) as send, \
             mock.patch.object(mod, "mark_pushed"):
            mod.main()
        return send.call_args[0][0]

    def test_daily_news_uses_edition_and_data_timestamp(self):
        msg = self._message(daily_news)
        self.assertIn("档期", msg)
        self.assertIn("数据时点", msg)
        self.assertNotIn("上个交易日大盘回顾", msg)
        self.assertNotIn("今日财经要闻", msg)

    def test_evening_news_uses_edition_and_data_timestamp(self):
        msg = self._message(evening_news)
        self.assertIn("档期", msg)
        self.assertIn("数据时点", msg)
        self.assertNotIn("今日收盘行情", msg)


class TestNewsFreshness(unittest.TestCase):
    """新闻卡片必须能回答「这条消息是什么时候的」。

    旧版 _news_lines 只取 title/intro，滚动榜里混进几天前的置顶稿、
    早间档与晚间档推同一批新闻都无法发现，读者也无从判断时效。
    """

    NOW = 1_794_731_310  # 2026-11-15 16:28:30 北京时间

    def test_timestamp_parsing_from_real_source_fields(self):
        from utils import biga_sources as bs

        self.assertEqual(bs._news_timestamp({"ctime": "1794731310"}), 1794731310)
        self.assertEqual(bs._news_timestamp({"rtime": "1794731341"}), 1794731341)
        self.assertEqual(bs._news_timestamp({"intime": "1794731310"}), 1794731310)
        self.assertEqual(bs._news_timestamp({"ctime": "1794731310000"}), 1794731310)
        self.assertEqual(bs._news_timestamp({"time": "2026-11-15 16:28"}), 1794731280)
        self.assertIsNone(bs._news_timestamp({}))

    def test_stale_and_duplicate_items_are_dropped(self):
        from utils import biga_sources as bs

        fresh = self.NOW - 60
        old = self.NOW - 40 * 3600
        items = [
            ("今日要闻", "摘要", fresh),
            ("今日要闻", "同一条的重复抓取", fresh),
            ("三天前的旧闻", "", old),
            ("上游没给时间字段的稿子", "摘要", None),
        ]
        lines = bs._news_lines(items, count=10, now_ts=self.NOW)
        text = "\n".join(lines)
        self.assertIn("今日要闻", text)
        self.assertIn("上游没给时间字段的稿子", text)
        self.assertNotIn("三天前的旧闻", text)
        self.assertEqual(sum(1 for line in lines if line.startswith("  • ")), 2)
        self.assertIn("[11-15 16:27]", text, "卡片必须标出每条新闻的发布时间")

    def test_long_intro_is_clipped_with_ellipsis(self):
        """截断必须补省略号。

        新浪的 intro 由上游截好并自带 "..."，但同花顺兜底源的 digest 常见
        110~270 字，旧实现直接 [:110] 硬切，行尾连省略号都没有，读者分不清
        是消息发一半断了还是原文到此结束。
        """
        from utils import biga_sources as bs

        lines = bs._news_lines(
            [("长摘要稿", "要" * 300, self.NOW - 60)], count=1, now_ts=self.NOW)
        body = lines[1].strip()
        self.assertTrue(body.endswith("…"), body[-10:])
        self.assertLessEqual(len(body), bs.NEWS_INTRO_MAX)

        # 未超长的摘要原样保留，不画蛇添足
        lines = bs._news_lines(
            [("短摘要稿", "新浪给的完整句子。", self.NOW - 60)], count=1, now_ts=self.NOW)
        self.assertEqual(lines[1].strip(), "新浪给的完整句子。")

        # 上游已带 "..." 的条目，截断处不叠加成 "...."
        clipped = bs._clip_intro("新" * 108 + "...")
        self.assertNotIn("..", clipped)
        self.assertLessEqual(len(clipped), bs.NEWS_INTRO_MAX)

    def test_all_stale_raises_so_provider_can_fall_back(self):
        from utils import biga_sources as bs

        old = self.NOW - 40 * 3600
        with self.assertRaisesRegex(Exception, "过期|空列表"):
            bs._news_lines([("旧闻", "", old)], count=10, now_ts=self.NOW)

    def test_every_news_provider_passes_timestamps(self):
        """新增新闻源时别忘了把发布时间传进渲染层。"""
        with open(os.path.join(ROOT, "utils", "biga_sources.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertGreaterEqual(src.count("_news_timestamp(n)"), 3)


if __name__ == "__main__":
    unittest.main()
