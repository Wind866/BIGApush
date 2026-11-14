"""图片推送链路的兜底行为测试（不联网、不需要 matplotlib / requests）。

为什么单独测这个：群里最怕「静默降级」——图片挂掉后既不报错也不发文本，
用户只看到机器人没反应。所以这里把三条路都钉住：
  1. 图片全成功 → 只发图，不重复刷文本；
  2. 部分失败   → 只补发失败那几只的文本，不重复已发出去的；
  3. 全部失败/缺凭证 → 整段文本兜底，且返回值仍算「发成功」。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _FakeResponse:
    def __init__(self, payload, status: int = 200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeRequests(types.ModuleType):
    """只实现 push 链路会用到的 post。"""

    def __init__(self):
        super().__init__("requests")
        self.uploads = []
        self.messages = []
        self.webhook_calls = []
        self.image_fail = False

    def post(self, url, **kwargs):
        if "/im/v1/images" in url:
            self.uploads.append(kwargs.get("files"))
            if self.image_fail:
                return _FakeResponse({"code": 234001, "msg": "Invalid request param"})
            return _FakeResponse({"code": 0, "data": {"image_key": f"img_{len(self.uploads)}"}})
        if "/im/v1/messages" in url:
            self.messages.append(kwargs.get("json") or {})
            return _FakeResponse({"code": 0, "data": {}})
        # webhook
        self.webhook_calls.append(kwargs.get("json") or {})
        return _FakeResponse({"code": 0})


def _load_stock_query(fake_requests, render_fn):
    """按文件加载 stock_query，并把它依赖的两个重模块换成假的。"""
    sys.modules["requests"] = fake_requests

    card_mod = types.ModuleType("utils.stock_card")
    card_mod.render_stock_card = render_fn
    sys.modules["utils.stock_card"] = card_mod

    root_in_path = ROOT not in sys.path
    if root_in_path:
        sys.path.insert(0, ROOT)
    try:
        spec = importlib.util.spec_from_file_location(
            "stock_query_under_test", os.path.join(ROOT, "stock_query.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if root_in_path and ROOT in sys.path:
            sys.path.remove(ROOT)
    return mod


def _spec(code: str) -> dict:
    return {"code": code, "name": f"股票{code}"}


class PushCardsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cards_")
        os.environ["CARD_DIR"] = self.tmp
        os.environ["FEISHU_CHAT_ID"] = "oc_test"
        os.environ.pop("FEISHU_WEBHOOK", None)
        self.fake = _FakeRequests()
        self.rendered = []

    def tearDown(self):
        for key in ("CARD_DIR", "FEISHU_CHAT_ID", "FEISHU_WEBHOOK"):
            os.environ.pop(key, None)

    def _render_ok(self, path, **kwargs):
        self.rendered.append(kwargs.get("code"))
        with open(path, "wb") as fh:
            fh.write(b"\x89PNG fake")
        return True

    def test_all_images_sent_without_text_duplicate(self):
        mod = _load_stock_query(self.fake, self._render_ok)
        mod._feishu_token = lambda: "tok"
        pairs = [(0, _spec("600519")), (1, _spec("000001"))]
        blocks = ["块A", "块B"]

        sent = mod.push_cards(pairs, blocks, "完整文本")

        self.assertTrue(sent)
        self.assertEqual(len(self.fake.uploads), 2, "两张图都应上传")
        kinds = [m.get("msg_type") for m in self.fake.messages]
        self.assertEqual(kinds, ["image", "image"], "只发图片，不该再发文本")
        self.assertEqual(self.fake.webhook_calls, [])

    def test_partial_failure_only_resends_failed_block(self):
        def render(path, **kwargs):
            if kwargs.get("code") == "000001":
                return False
            with open(path, "wb") as fh:
                fh.write(b"\x89PNG fake")
            return True

        mod = _load_stock_query(self.fake, render)
        mod._feishu_token = lambda: "tok"
        pairs = [(0, _spec("600519")), (1, _spec("000001"))]
        blocks = ["块600519", "块000001"]

        sent = mod.push_cards(pairs, blocks, "完整文本")

        self.assertTrue(sent)
        texts = [m for m in self.fake.messages if m.get("msg_type") == "text"]
        self.assertEqual(len(texts), 1, "失败那一只应补发一条文本")
        self.assertIn("000001", texts[0]["content"])
        self.assertNotIn("块600519", texts[0]["content"], "已发图的不要重复")

    def test_all_render_failed_falls_back_to_full_text(self):
        mod = _load_stock_query(self.fake, lambda path, **kwargs: False)
        mod._feishu_token = lambda: "tok"

        sent = mod.push_cards([(0, _spec("600519"))], ["块A"], "完整文本")

        self.assertTrue(sent)
        self.assertEqual(self.fake.uploads, [])
        texts = [m for m in self.fake.messages if m.get("msg_type") == "text"]
        self.assertEqual(len(texts), 1)
        self.assertIn("完整文本", texts[0]["content"])

    def test_upload_rejected_falls_back_to_text(self):
        self.fake.image_fail = True
        mod = _load_stock_query(self.fake, self._render_ok)
        mod._feishu_token = lambda: "tok"

        sent = mod.push_cards([(0, _spec("600519"))], ["块A"], "完整文本")

        self.assertTrue(sent)
        texts = [m for m in self.fake.messages if m.get("msg_type") == "text"]
        self.assertEqual(len(texts), 1, "上传失败应退回文本而不是静默")

    def test_no_chat_id_keeps_text_path(self):
        os.environ.pop("FEISHU_CHAT_ID", None)
        os.environ["FEISHU_WEBHOOK"] = "https://example.invalid/hook"
        mod = _load_stock_query(self.fake, self._render_ok)
        mod._feishu_token = lambda: "tok"

        sent = mod.push_cards([(0, _spec("600519"))], ["块A"], "完整文本")

        self.assertTrue(sent)
        self.assertEqual(self.fake.uploads, [], "没有会话 id 时不该尝试发图")
        self.assertEqual(len(self.fake.webhook_calls), 1, "应走 webhook 文本兜底")


if __name__ == "__main__":
    unittest.main()
