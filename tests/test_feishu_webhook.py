"""飞书发送层 / 告警通道 / 工作流静态回归测试。

针对本轮修复的几类「不报错但完全失效」的问题：
1. 裸 curl 告警不发 sign、不解析业务码 —— 开加签后永久哑掉；
2. workflow inputs 插进 run 源码 —— 表达式注入；
3. 单档 cron 兜底 —— GitHub 丢档即整天无消息。
"""
from __future__ import annotations

import base64
import glob
import hashlib
import hmac
import io
import json
import os
import sys
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from utils import feishu_webhook as fw  # noqa: E402


class TestSign(unittest.TestCase):
    def test_gen_sign_matches_official_algorithm(self):
        secret, ts = "sec_abc123", "1700000000"
        expected = base64.b64encode(
            hmac.new(f"{ts}\n{secret}".encode(), digestmod=hashlib.sha256).digest()
        ).decode()
        self.assertEqual(fw.gen_sign(secret, ts), expected)

    def test_sign_payload_adds_fields_only_with_secret(self):
        plain = fw.sign_payload({"msg_type": "text"}, "")
        self.assertEqual(plain, {"msg_type": "text"})

        signed = fw.sign_payload({"msg_type": "text"}, "sec")
        self.assertIn("timestamp", signed)
        self.assertIn("sign", signed)
        self.assertEqual(signed["sign"], fw.gen_sign("sec", signed["timestamp"]))
        # 不能就地修改调用方的 payload
        src = {"msg_type": "text"}
        fw.sign_payload(src, "sec")
        self.assertEqual(src, {"msg_type": "text"})

    def test_response_ok(self):
        for good in ({"code": 0}, {"StatusCode": 0}, {}, {"msg": "success"}):
            self.assertTrue(fw.response_ok(good), good)
        for bad in ({"code": 19021}, {"code": 19024}, {"StatusCode": 1}, None, "ok"):
            self.assertFalse(fw.response_ok(bad), bad)


class TestPostJson(unittest.TestCase):
    def _fake_resp(self, payload):
        resp = mock.MagicMock()
        resp.read.return_value = json.dumps(payload).encode()
        resp.__enter__.return_value = resp
        return resp

    def test_signed_request_and_success(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return self._fake_resp({"code": 0})

        with mock.patch.object(fw.urllib.request, "urlopen", fake_urlopen):
            result = fw.post_json("https://example.com/hook",
                                  {"msg_type": "text"}, "sec")
        self.assertEqual(result, {"code": 0})
        self.assertIn("sign", captured["body"])
        self.assertIn("timestamp", captured["body"])

    def test_network_error_retries_once(self):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(1)
            if len(calls) == 1:
                raise URLError("reset")
            return self._fake_resp({"code": 0})

        with mock.patch.object(fw.urllib.request, "urlopen", fake_urlopen), \
             mock.patch.object(fw.time, "sleep"):
            self.assertEqual(fw.post_json("https://x", {"a": 1}), {"code": 0})
        self.assertEqual(len(calls), 2)

    def test_4xx_not_retried(self):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(1)
            raise HTTPError("https://x", 400, "bad", {}, io.BytesIO())

        with mock.patch.object(fw.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(HTTPError):
                fw.post_json("https://x", {"a": 1})
        self.assertEqual(len(calls), 1)

    def test_empty_webhook_fails(self):
        with self.assertRaises(ValueError):
            fw.post_json("", {"a": 1})


class TestAlertBehaviour(unittest.TestCase):
    def test_unconfigured_webhook_is_never_success(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            ok, detail = fw.alert_text("hello")
        self.assertFalse(ok)
        self.assertEqual(detail, "未配置 webhook")

    def test_sign_error_is_reported_as_failure(self):
        env = {"FEISHU_WEBHOOK": "https://x", "FEISHU_SECRET": "s"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(fw, "send_text", return_value={"code": 19021, "msg": "sign fail"}):
            ok, detail = fw.alert_text("hello")
        self.assertFalse(ok)
        self.assertIn("19021", detail)

    def test_alert_credentials_priority(self):
        env = {"ALERT_WEBHOOK": "https://alert", "ALERT_SECRET": "sa",
               "FEISHU_WEBHOOK": "https://biz", "FEISHU_SECRET": "sb"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(fw.resolve_credentials(alert=True), ("https://alert", "sa"))
            self.assertEqual(fw.resolve_credentials(), ("https://biz", "sb"))
        with mock.patch.dict(os.environ, {"FEISHU_WEBHOOK": "https://biz"}, clear=True):
            self.assertEqual(fw.resolve_credentials(alert=True), ("https://biz", ""))


class TestWorkflowStaticGuards(unittest.TestCase):
    """把 CI 里的静态门禁也在本地跑一遍，避免「改回去」没人发现。"""

    def test_all_workflows_parse(self):
        import yaml
        files = sorted(glob.glob(os.path.join(ROOT, ".github/workflows/*.yml")))
        self.assertTrue(files)
        for path in files:
            with open(path, encoding="utf-8") as fh:
                self.assertIsInstance(yaml.safe_load(fh), dict, path)

    def test_no_bare_curl_alert(self):
        """失败告警走 ./.github/actions/alert，动作内部再复用带签名的 scripts/alert.py。"""
        action = open(os.path.join(ROOT, ".github/actions/alert/action.yml"),
                      encoding="utf-8").read()
        self.assertIn("scripts/alert.py", action,
                      "复合告警动作必须复用 alert.py，否则会丢加签与业务码校验")
        for path in glob.glob(os.path.join(ROOT, ".github/workflows/*.yml")):
            text = open(path, encoding="utf-8").read()
            name = os.path.basename(path)
            if "if: failure()" in text:
                self.assertIn("./.github/actions/alert", text,
                              f"{name} 的失败告警没有走统一入口")
            self.assertNotIn('curl -sS -X POST "$HOOK"', text, name)

    def test_no_input_interpolation_in_run_blocks(self):
        import yaml
        bad = []
        for path in sorted(glob.glob(os.path.join(ROOT, ".github/workflows/*.yml"))):
            with open(path, encoding="utf-8") as fh:
                doc = yaml.safe_load(fh)
            for job in (doc.get("jobs") or {}).values():
                for step in job.get("steps", []):
                    if "${{ inputs." in (step.get("run") or ""):
                        bad.append((os.path.basename(path), step.get("name")))
        self.assertEqual(bad, [], f"发现表达式注入面：{bad}")

    def test_critical_workflows_have_fallback_crons(self):
        import yaml
        for name in ("morning-news.yml", "evening-news.yml",
                     "intraday-selection.yml", "selection.yml"):
            path = os.path.join(ROOT, ".github/workflows", name)
            with open(path, encoding="utf-8") as fh:
                doc = yaml.safe_load(fh)
            crons = (doc.get("on") or doc.get(True) or {}).get("schedule") or []
            self.assertGreaterEqual(len(crons), 2, f"{name} 缺少兜底 cron")

    def test_worker_notify_signs_and_checks_code(self):
        src = open(os.path.join(ROOT, "cloudflare/worker.js"), encoding="utf-8").read()
        self.assertIn("payload.sign = await feishuSign", src)
        self.assertIn("code !== 0", src)
        self.assertIn("ALERT_SECRET", src)

    def test_ci_runs_on_main_code_changes(self):
        import re
        src = open(os.path.join(ROOT, ".github/workflows/ci.yml"), encoding="utf-8").read()
        # 注释里提到旧写法是允许的，只拦真正的配置行
        self.assertIsNone(re.search(r"^\s+branches-ignore:", src, re.M))
        self.assertIn("paths:", src)


if __name__ == "__main__":
    unittest.main()
