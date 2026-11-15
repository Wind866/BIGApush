#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""档期对账的结论判定 —— 「触发」不等于「成功」。

背景：旧实现收集命中时只取 created_at，不看 conclusion。于是一个 failure 或
cancelled 的运行也会被记成「✅ 准点」，报表最后打印「无已覆盖范围内的漏跑」——
对账全绿，当天其实一条都没推出去。这个测试把它钉住。

用假的 runs 数据驱动 build_rows，不联网、不需要 token。
"""

import importlib.util
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_audit_module():
    spec = importlib.util.spec_from_file_location(
        "schedule_audit_under_test", ROOT / "scripts" / "schedule_audit.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestAuditConclusion(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sa = _load_audit_module()
        # 本仓库 + 一个 15:43 工作日档期，目标指向本仓库的 selection.yml
        cls.sa.parse_plan = lambda: [{
            "at": "15:43", "name": "收盘选股", "days": "weekday",
            "targets": ["BIGApush/selection.yml"],
        }]

    def setUp(self):
        self._env_backup = {k: os.environ.get(k) for k in
                            ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_REPOSITORY", "AUDIT_PAT")}
        os.environ["GITHUB_TOKEN"] = "dummy"
        os.environ["GITHUB_REPOSITORY"] = "Wind866/BIGApush"
        os.environ.pop("AUDIT_PAT", None)

    def tearDown(self):
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _rows_with(self, runs):
        """把给定的 run 列表喂给 build_rows，返回该档期的行。"""
        def fake_api_get(path, token):
            if "/runs" not in path:
                return {"id": 999}
            return {"workflow_runs": runs}

        self.sa.api_get = fake_api_get
        rows, _covered, _expected = self.sa.build_rows("2026-11-13")  # 周五，工作日
        self.assertEqual(len(rows), 1)
        return rows[0]

    @staticmethod
    def _run(created_at, conclusion, status="completed"):
        return {"workflow_id": 999, "event": "schedule",
                "created_at": created_at, "conclusion": conclusion, "status": status}

    def test_success_is_on_time(self):
        row = self._rows_with([self._run("2026-11-13T07:43:10Z", "success")])
        self.assertEqual(row["status"], "✅ 准点")
        self.assertIn("成功 1 次", row["note"])

    def test_failure_is_not_counted_as_on_time(self):
        """失败必须判红 —— 这是本次修复的核心。"""
        row = self._rows_with([self._run("2026-11-13T07:43:10Z", "failure")])
        self.assertEqual(row["status"], "❌ 触发但失败")
        self.assertTrue(row["status"].startswith("❌"), "必须计入 missing，让对账退出码非 0")

    def test_cancelled_is_not_counted_as_on_time(self):
        row = self._rows_with([self._run("2026-11-13T07:43:10Z", "cancelled")])
        self.assertEqual(row["status"], "❌ 触发但失败")

    def test_in_progress_is_pending_not_missing(self):
        """还没跑完的运行既不算成功，也不该误判成漏跑。"""
        row = self._rows_with([self._run("2026-11-13T07:43:10Z", None, status="in_progress")])
        self.assertEqual(row["status"], "🔄 运行中")
        self.assertFalse(row["status"].startswith("❌"), "运行中不能计入 missing")

    def test_no_runs_is_missing(self):
        row = self._rows_with([])
        self.assertEqual(row["status"], "❌ 未到")

    def test_late_success_uses_the_successful_run(self):
        """先失败后补跑成功：以成功那次算偏差，不该被首次失败卡住。"""
        row = self._rows_with([
            self._run("2026-11-13T07:43:10Z", "failure"),
            self._run("2026-11-13T08:50:00Z", "success"),
        ])
        self.assertEqual(row["status"], "⚠️ 延迟")
        self.assertEqual(row["delay"], "+67 分")
        self.assertEqual(row["actual"], "16:50:00")


if __name__ == "__main__":
    unittest.main()
