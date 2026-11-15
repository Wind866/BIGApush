"""档期表与 GitHub cron 兜底一致性回归（CI 门禁）。

`cloudflare/worker.js` 的 PLAN 是全部定时任务的唯一事实来源；各 workflow 里的
`on.schedule` 是 Worker 失效时唯一的兜底。二者一旦漂移（改了档期表却忘了改
cron，或反过来），Worker 正常时看不出来，一旦 Worker / PAT 出问题就会直接漏档，
而且没有任何告警。这条用例把漂移变成 CI 红灯。

只校验本仓库能读到的 workflow：PLAN 里 limit-up-relay 与 etf-feishu-push 的
目标由各自仓库负责，这里跳过。
"""
from __future__ import annotations

import glob
import os
import re
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(ROOT, "cloudflare", "worker.js")
WF_DIR = os.path.join(ROOT, ".github", "workflows")


def parse_plan() -> list[tuple[str, list[str]]]:
    with open(WORKER, encoding="utf-8") as fh:
        src = fh.read()
    body = src.split("const PLAN = [", 1)[1].split("\n];", 1)[0]
    slots: list[tuple[str, list[str]]] = []
    for block in re.findall(r"\{(.*?)\}", body, re.S):
        at = re.search(r'at:\s*"(\d{2}:\d{2})"', block)
        targets = re.search(r"targets:\s*\[([^\]]*)\]", block, re.S)
        if not (at and targets):
            continue
        slots.append((at.group(1), re.findall(r'"([^"]+)"', targets.group(1))))
    return slots


def workflow_crons(path: str) -> list[str]:
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    # PyYAML 按 YAML 1.1 解析，裸 on: 会变成布尔 True
    trigger = doc.get("on", doc.get(True)) or {}
    schedules = trigger.get("schedule") or [] if isinstance(trigger, dict) else []
    out = []
    for item in schedules:
        cron = item.get("cron") if isinstance(item, dict) else item
        if cron:
            out.append(str(cron))
    return out


def bj_hhmm(cron: str) -> str | None:
    """GitHub cron 走 UTC，换算成北京时间 HH:MM（只比时刻，不比星期）。"""
    parts = cron.split()
    if len(parts) < 2:
        return None
    try:
        minute, hour = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    return f"{(hour + 8) % 24:02d}:{minute:02d}"


class TestPlanCronConsistency(unittest.TestCase):
    def test_plan_is_parseable(self):
        slots = parse_plan()
        self.assertGreaterEqual(len(slots), 9, "PLAN 解析结果异常（档期数量骤减）")
        for at, targets in slots:
            self.assertRegex(at, r"^\d{2}:\d{2}$")
            self.assertTrue(targets, f"{at} 档期没有任何 target")

    def test_every_local_slot_has_a_matching_fallback_cron(self):
        """PLAN 里指向本仓库的每个档期，都必须至少有一条 UTC cron 换算后命中。"""
        checked = 0
        for at, targets in parse_plan():
            for target in targets:
                repo, workflow = target.split("/", 1)
                if repo != "BIGApush":
                    continue
                path = os.path.join(WF_DIR, workflow)
                self.assertTrue(os.path.isfile(path),
                                f"PLAN 指向了不存在的工作流：{target}")
                crons = workflow_crons(path)
                self.assertTrue(crons, f"{workflow} 没有任何 schedule 兜底")
                bj_times = {bj_hhmm(c) for c in crons}
                self.assertIn(
                    at, bj_times,
                    f"{workflow} 缺少 {at}（北京）的 cron 兜底；现有：{sorted(x for x in bj_times if x)}",
                )
                checked += 1
        self.assertGreater(checked, 0, "没有校验到任何本仓库档期，用例形同虚设")

    def test_all_workflow_yaml_is_parseable(self):
        files = sorted(glob.glob(os.path.join(WF_DIR, "*.yml")))
        self.assertTrue(files)
        for path in files:
            with self.subTest(path=os.path.basename(path)):
                with open(path, encoding="utf-8") as fh:
                    yaml.safe_load(fh)


if __name__ == "__main__":
    unittest.main()
