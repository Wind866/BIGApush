"""Worker 部署可观测性回归测试：保证 /health 能区分线上跑的是哪一版。

改 worker.js 后必须递增 BUILD 并同步更新 cloudflare/README.md 里记录的版本，
否则运维侧无法判断 Cloudflare 上部署的是不是仓库里这一版（历史上真出过这事）。
"""
from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(ROOT, "cloudflare", "worker.js")
README = os.path.join(ROOT, "cloudflare", "README.md")


class WorkerVersionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(WORKER, encoding="utf-8") as fh:
            cls.js = fh.read()
        with open(README, encoding="utf-8") as fh:
            cls.doc = fh.read()

    def _build(self) -> str:
        m = re.search(r'const BUILD = "([^"]+)"', self.js)
        self.assertIsNotNone(m, "worker.js 缺少 BUILD 常量")
        return m.group(1)

    def test_health_exposes_build_and_alert_fields(self):
        for field in ("build: BUILD", "alertSignSupported: true",
                      "alertSecretConfigured:", "alertConfigured:", "tokenValid"):
            self.assertIn(field, self.js, f"/health 缺少字段 {field}")

    def test_notify_signs_and_checks_business_code(self):
        self.assertIn("async function notify", self.js)
        notify = self.js[self.js.index("async function notify"):]
        notify = notify[:notify.index("async function authHeaders")]
        self.assertIn("ALERT_SECRET", notify)
        self.assertIn("payload.sign", notify)
        self.assertIn("19021", notify)  # 飞书业务码错误不能被当成发送成功

    def test_readme_records_current_build(self):
        build = self._build()
        self.assertIn(build, self.doc,
                      "改了 BUILD 却没同步 README 的核对示例，运维会比对到错误版本")


if __name__ == "__main__":
    unittest.main()
