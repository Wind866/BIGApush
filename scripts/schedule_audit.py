#!/usr/bin/env python3
"""当日 9 档期「应到 / 实到」对账 —— 把「按时完成」变成可核对的数字。

档期表直接解析 cloudflare/worker.js 的 PLAN（唯一事实来源），
实到记录来自 GitHub Actions runs API。输出：
  * docs/schedule-audit.md      可上传为 artifact 的 Markdown 报告
  * $GITHUB_STEP_SUMMARY        Actions 运行页直接可见
  * stdout                      本地手工核对

覆盖范围说明
------------
Actions 内置 GITHUB_TOKEN 只能读本仓库。要核对另外两个仓库（尤其私有的
etf-feishu-push），需配置 Secret AUDIT_PAT（fine-grained PAT，Actions: read）。
未覆盖的仓库一律标「⬜ 未覆盖」而**不判失败**，避免把「查不到」误报成「没跑」。

用法：
    python scripts/schedule_audit.py                 # 今天（北京时间）
    python scripts/schedule_audit.py --date 2026-11-16
    AUDIT_DATE=2026-11-16 python scripts/schedule_audit.py
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, time as dtime, timedelta, timezone

BJ = timezone(timedelta(hours=8))
API = "https://api.github.com"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = "Wind866"


def parse_plan() -> list[dict]:
    with open(os.path.join(ROOT, "cloudflare", "worker.js"), encoding="utf-8") as fh:
        src = fh.read()
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


def due_today(slot: dict, dow_js: int) -> bool:
    """dow_js 与 worker.js 的 getUTCDay() 对齐：0=周日，1=周一 ... 6=周六。"""
    rule = slot["days"]
    if rule == "all":
        return True
    if rule == "weekday":
        return 1 <= dow_js <= 5
    try:
        return dow_js == int(rule)
    except ValueError:
        return False


def api_get(path: str, token: str) -> dict:
    req = urllib.request.Request(API + path, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "schedule-audit",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def build_rows(day: str) -> tuple[list[dict], int, int]:
    dt = datetime.fromisoformat(day).replace(tzinfo=BJ)
    dow_js = (dt.weekday() + 1) % 7
    pat = (os.getenv("AUDIT_PAT") or "").strip()
    builtin = (os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or "").strip()
    this_repo = (os.getenv("GITHUB_REPOSITORY") or f"{OWNER}/BIGApush").strip()

    rows: list[dict] = []
    covered = 0
    for slot in parse_plan():
        due = due_today(slot, dow_js)
        hour, minute = (int(x) for x in slot["at"].split(":"))
        planned = datetime.combine(dt.date(), dtime(hour, minute), tzinfo=BJ)
        for target in slot["targets"]:
            repo, workflow = target.split("/", 1)
            full_repo = f"{OWNER}/{repo}"
            token = pat or (builtin if full_repo == this_repo else "")
            row = {
                "slot": slot["at"], "name": slot["name"], "target": target,
                "due": due, "covered": bool(token), "actual": "—",
                "delay": "—", "status": "", "note": "",
            }
            if not due:
                row["status"] = "— 非当日档期"
                rows.append(row)
                continue
            if not token:
                row["status"] = "⬜ 未覆盖"
                row["note"] = "需配置 AUDIT_PAT 才能读此仓库的 Actions 记录"
                rows.append(row)
                continue
            covered += 1
            try:
                info = api_get(f"/repos/{full_repo}/actions/workflows/{workflow}", token)
                wid = info["id"]
                runs = api_get(f"/repos/{full_repo}/actions/runs?per_page=100", token)["workflow_runs"]
            except Exception as exc:  # noqa: BLE001 - 查询失败不等于任务失败
                row["status"] = "⬜ 未覆盖"
                row["note"] = f"API 查询失败：{str(exc)[:80]}"
                covered -= 1
                rows.append(row)
                continue

            hits = []
            for run in runs:
                if run.get("workflow_id") != wid:
                    continue
                if run.get("event") not in ("workflow_dispatch", "schedule"):
                    continue
                created = datetime.fromisoformat(
                    run["created_at"].replace("Z", "+00:00")
                ).astimezone(BJ)
                if created.date().isoformat() == day:
                    hits.append(created)
            if not hits:
                row["status"] = "❌ 未到"
            else:
                first = min(hits)
                delay = int((first - planned).total_seconds() / 60)
                row["actual"] = first.strftime("%H:%M:%S")
                row["delay"] = f"{delay:+d} 分"
                row["status"] = "✅ 准点" if -5 <= delay <= 5 else "⚠️ 延迟"
                row["note"] = f"当日命中 {len(hits)} 次触发"
            rows.append(row)
    return rows, covered, sum(1 for r in rows if r["due"])


def render(rows: list[dict], day: str, expected: int, covered: int) -> str:
    arrived = sum(1 for r in rows if r["status"].startswith(("✅", "⚠️")))
    missing = sum(1 for r in rows if r["status"].startswith("❌"))
    lines = [
        f"# 档期对账 · {day}（北京时间）",
        "",
        f"- 当日应到：**{expected}** 个目标 · 可核对：**{covered}** 个",
        f"- 实到：**{arrived}** · 未到：**{missing}**",
        "- 档期表来源：`cloudflare/worker.js` 的 `PLAN`；「⬜ 未覆盖」表示缺 `AUDIT_PAT`，不代表任务没跑",
        "",
        "| 档期 | 任务 | 目标 | 实到 | 偏差 | 结论 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        note = f" ({r['note']})" if r["note"] else ""
        lines.append(
            f"| {r['slot']} | {r['name']} | `{r['target']}` | {r['actual']} | "
            f"{r['delay']} | {r['status']}{note} |"
        )
    lines += [
        "",
        "> ✅ 偏差 ≤5 分钟；⚠️ 有触发但延迟（GitHub cron 兜底常晚数小时）；"
        "❌ 应到且可核对，但当天没有任何触发记录。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="当日档期应到/实到对账")
    ap.add_argument("--date", default=os.getenv("AUDIT_DATE", ""),
                    help="北京时间日期 YYYY-MM-DD，默认今天")
    args = ap.parse_args()
    day = args.date or datetime.now(BJ).date().isoformat()

    rows, covered, expected = build_rows(day)
    report = render(rows, day, expected, covered)
    print(report)

    # 即使仓库里原本没有 docs/ 也要生成，否则 workflow 的 artifact 永远上传不到东西
    out_dir = os.path.join(ROOT, "docs")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "schedule-audit.md"), "w", encoding="utf-8") as fh:
        fh.write(report)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(report)

    missing = sum(1 for r in rows if r["status"].startswith("❌"))
    uncovered = sum(1 for r in rows if r["status"].startswith("⬜") and r["due"])
    if uncovered:
        print(f"::warning::{uncovered} 个当日档期未能核对（缺 AUDIT_PAT），请补配后重跑")
    if missing:
        print(f"::error::{missing} 个当日应到档期没有任何触发记录")
        return 1
    print("档期对账完成：无已覆盖范围内的漏跑")
    return 0


if __name__ == "__main__":
    sys.exit(main())
