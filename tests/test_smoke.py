"""BIGApush 工程健康回归测试（纯标准库，不导入业务模块，不联网）。"""
from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache"}


def read(path: str) -> str:
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        return fh.read()


def parse_plan() -> list[dict]:
    """从 worker.js 里解析出唯一的档期表 PLAN。"""
    src = read(os.path.join("cloudflare", "worker.js"))
    body = src.split("const PLAN = [", 1)[1].split("\n];", 1)[0]
    slots = []
    for block in re.findall(r"\{(.*?)\}", body, re.S):
        at = re.search(r'at:\s*"(\d{2}:\d{2})"', block)
        name = re.search(r'name:\s*"([^"]+)"', block)
        days = re.search(r"days:\s*([^,\n]+)", block)
        targets = re.search(r"targets:\s*\[([^\]]*)\]", block, re.S)
        if not (at and name and days and targets):
            continue
        slots.append({
            "at": at.group(1),
            "name": name.group(1),
            "days": days.group(1).strip().strip('"').strip("'"),
            "targets": re.findall(r'"([^"]+)"', targets.group(1)),
        })
    return slots


class TestPythonSyntax(unittest.TestCase):
    """任何 .py 有语法错误都必须让 CI 变红（此前 f-string 缺引号无人发现）。"""

    def test_every_python_file_compiles(self):
        broken = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                path = os.path.join(dirpath, filename)
                with open(path, encoding="utf-8", errors="ignore") as fh:
                    source = fh.read()
                try:
                    compile(source, path, "exec")
                except SyntaxError as exc:
                    broken.append(f"{os.path.relpath(path, ROOT)}:{exc.lineno} {exc.msg}")
        self.assertEqual(broken, [], "存在语法错误：\n" + "\n".join(broken))


class TestWorkerPlan(unittest.TestCase):
    def test_nine_unique_slots(self):
        plan = parse_plan()
        self.assertEqual(len(plan), 9, f"档期数量应为 9，实际 {len(plan)}")
        times = [s["at"] for s in plan]
        self.assertEqual(len(times), len(set(times)), "档期时间有重复")

    def test_every_slot_has_target(self):
        for slot in parse_plan():
            self.assertTrue(slot["targets"], f"{slot['at']} 没有目标 workflow")

    def test_days_values_are_valid(self):
        for slot in parse_plan():
            self.assertIn(slot["days"], {"all", "weekday", "1", "2"},
                          f"{slot['at']} 的 days 非法：{slot['days']}")

    def test_local_workflow_files_exist(self):
        """PLAN 里指向 BIGApush 的 workflow 文件必须真实存在。"""
        for slot in parse_plan():
            for target in slot["targets"]:
                repo, workflow = target.split("/", 1)
                if repo != "BIGApush":
                    continue
                self.assertTrue(
                    os.path.exists(os.path.join(ROOT, ".github", "workflows", workflow)),
                    f"档期 {slot['at']} 指向不存在的 {target}",
                )

    def test_readme_lists_every_slot(self):
        readme = read(os.path.join("cloudflare", "README.md"))
        for slot in parse_plan():
            self.assertIn(slot["at"], readme, f"README 档期表缺少 {slot['at']}")

    def test_workflows_accept_dispatch(self):
        """Worker 靠 workflow_dispatch 触发，每个目标 workflow 都必须声明它。"""
        for slot in parse_plan():
            for target in slot["targets"]:
                repo, workflow = target.split("/", 1)
                if repo != "BIGApush":
                    continue
                with self.subTest(target=target):
                    self.assertIn(
                        "workflow_dispatch",
                        read(os.path.join(".github", "workflows", workflow)),
                        f"{target} 缺少 workflow_dispatch",
                    )


class TestSchedulerReliability(unittest.TestCase):
    def test_scheduled_uses_cron_time_not_wall_clock(self):
        """Date.now() 跨分钟会精确错过 HH:MM 档期，必须用 event.scheduledTime。"""
        src = read(os.path.join("cloudflare", "worker.js"))
        scheduled = src.split("async scheduled(", 1)[1].split("async fetch(", 1)[0]
        self.assertIn("event.scheduledTime", scheduled)
        self.assertNotIn("beijingNow(Date.now())", scheduled)
        # /test 手动自测用当前钟点是合理的，不做限制

    def test_catch_up_exists(self):
        src = read(os.path.join("cloudflare", "worker.js"))
        self.assertIn("catchUp(env, bj)", src)

    def test_health_exposes_build(self):
        """线上 Worker 与仓库版本不一致过，/health 必须能报出版本号。"""
        src = read(os.path.join("cloudflare", "worker.js"))
        self.assertIn("build: BUILD", src)


class TestRepoHygiene(unittest.TestCase):
    def test_gitignore_allows_tests(self):
        gitignore = read(".gitignore")
        for line in gitignore.splitlines():
            stripped = line.strip()
            if stripped.startswith("test_") or stripped in ("tests/", "tests"):
                self.fail(f".gitignore 会吞掉测试文件：{stripped}")


if __name__ == "__main__":
    unittest.main()
