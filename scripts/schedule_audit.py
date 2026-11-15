#!/usr/bin/env python3
"""当日 9 档期「应到 / 实到」对账 —— 把「按时完成」变成可核对的数字。

「实到」只认 **conclusion=success** 的运行。旧版只看 created_at，一个失败或
被取消的 run 也会被记成「✅ 准点」，对账全绿却当天没推成功 —— 触发 ≠ 成功。

档期表直接解析 cloudflare/worker.js 的 PLAN（唯一事实来源），
实到记录来自 GitHub Actions runs API。输出：
  * docs/schedule-audit.md      可上传为 artifact 的 Markdown 报告
  * $GITHUB_STEP_SUMMARY        Actions 运行页直接可见
  * stdout                      本地手工核对

覆盖范围说明
------------
Actions 内置 GITHUB_TOKEN 只能读本仓库。另外两个仓库（etf-feishu-push /
limit-up-relay）都是**公开仓库**，可以匿名读 Actions 记录（60 次/小时，
9 个档期够用），所以未配 AUDIT_PAT 时不再整段放弃，而是自动降级为匿名查询：

  * 本仓库          → GITHUB_TOKEN
  * 其他公开仓库    → AUDIT_PAT（若配置）> 匿名
  * 私有仓库        → 必须有 AUDIT_PAT，否则标「⬜ 未覆盖」而不判失败

仍然区分「查不到」和「没跑」：查询失败只标未覆盖，绝不判成漏跑。

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

# 这些结论表示「已经触发但还没结束」，既不算成功也不能判失败
PENDING_CONCLUSIONS = {"", "in_progress", "queued", "waiting", "requested", "pending"}


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
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "schedule-audit",
    }
    # token 为空时走匿名请求：公开仓库的 Actions 记录本来就能匿名读，
    # 只是限额 60 次/小时（本脚本一次对账 9 个档期，够用）。
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(API + path, headers=headers)
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
            # 本仓库用内置 token；其他仓库优先 AUDIT_PAT，没有就匿名读
            # （公开仓库读 Actions 不需要鉴权，旧逻辑在这里直接放弃，
            #  于是 etf / relay 的档期长期挂着「⬜ 未覆盖」，漏跑根本发现不了）。
            token = pat or (builtin if full_repo == this_repo else "")
            row = {
                "slot": slot["at"], "name": slot["name"], "target": target,
                "due": due, "covered": True, "actual": "—",
                "delay": "—", "status": "", "note": "",
            }
            if not due:
                row["status"] = "— 非当日档期"
                rows.append(row)
                continue
            # 计划时刻还没到（白天查 22:13 的档期）不能算「未到」，否则对账天天报假警
            if planned > datetime.now(BJ):
                row["status"] = "⏳ 未到时刻"
                rows.append(row)
                continue
            covered += 1
            try:
                info = api_get(f"/repos/{full_repo}/actions/workflows/{workflow}", token)
                wid = info["id"]
                # 按「日期 + 具体 workflow」精确查询。旧版拉仓库级最近 100 条再按
                # workflow_id 过滤：本仓库 10 个工作流每天触发多次，100 条只够覆盖
                # 一两天；跨天对账时前一日 08:43 的记录早被挤出列表，会被误报成
                # 「❌ 未到」并触发一条假的漏跑告警。
                # 查询上限放到次日，是为了容纳晚间档最长 +10 小时的跨日延迟。
                next_day = (datetime.fromisoformat(day).date() + timedelta(days=1)).isoformat()
                runs = api_get(
                    f"/repos/{full_repo}/actions/workflows/{workflow}/runs"
                    f"?created={day}..{next_day}&per_page=100", token,
                )["workflow_runs"]
            except Exception as exc:  # noqa: BLE001 - 查询失败不等于任务失败
                row["status"] = "⬜ 未覆盖"
                row["note"] = f"API 查询失败：{str(exc)[:80]}"
                covered -= 1
                rows.append(row)
                continue

            # 档期窗口＝[计划时刻 − 5 分钟, 计划时刻 + 10 小时]，允许跨日。
            # 旧实现按「日历日」收集当天全部触发再取最早，会把*前一个档期*迟到的运行
            # 算到本档期头上：晚间新闻 22:13 次日 00:18 到达，会被记成「实到 00:18、
            # 偏差 −1314 分」；反过来当晚真的没跑时又要等到次日才看得见。
            # 上限定为 10 小时的依据：本账号 schedule 实测最长延迟 8h13m（收盘选股），
            # 原来的 +6h 会把这些「虽然迟到但确实跑了」的运行误报成「❌ 未到」。
            # 按 workflow_id 过滤，所以放宽窗口不会把别的工作流算进来。
            window_lo = planned - timedelta(minutes=5)
            window_hi = planned + timedelta(hours=10)
            hits = []
            for run in runs:
                if run.get("workflow_id") != wid:
                    continue
                if run.get("event") not in ("workflow_dispatch", "schedule"):
                    continue
                created = datetime.fromisoformat(
                    run["created_at"].replace("Z", "+00:00")
                ).astimezone(BJ)
                if window_lo <= created <= window_hi:
                    # 结论必须一起记：只认 created_at 的话，一个 failure / cancelled
                    # 的 run 也会被记成「✅ 准点」，于是「对账全绿」掩盖了当天根本
                    # 没推成功。触发 ≠ 成功。
                    hits.append((created, (run.get("conclusion") or run.get("status") or "").lower()))
            if not hits:
                row["status"] = "❌ 未到"
            else:
                ok = [h for h in hits if h[1] == "success"]
                running = [h for h in hits if h[1] in PENDING_CONCLUSIONS]
                if ok:
                    first = min(ok)[0]
                    delay = int((first - planned).total_seconds() / 60)
                    row["actual"] = first.strftime("%H:%M:%S")
                    row["delay"] = f"{delay:+d} 分"
                    row["status"] = "✅ 准点" if -5 <= delay <= 5 else "⚠️ 延迟"
                    row["note"] = f"当日命中 {len(hits)} 次触发，成功 {len(ok)} 次"
                elif running:
                    # 还没跑完（延迟到对账时刻之后），既不算准点也不算失败
                    first = min(hits)[0]
                    row["actual"] = first.strftime("%H:%M:%S")
                    row["status"] = "🔄 运行中"
                    row["note"] = "触发已到达但尚未结束，下次对账再判定"
                else:
                    first, concl = min(hits)
                    row["actual"] = first.strftime("%H:%M:%S")
                    row["delay"] = f"{int((first - planned).total_seconds() / 60):+d} 分"
                    row["status"] = "❌ 触发但失败"
                    row["note"] = (
                        f"当日命中 {len(hits)} 次触发，结论均为 "
                        f"{'/'.join(sorted({c or '未知' for _, c in hits}))}（未成功推送）"
                    )
            rows.append(row)
    return rows, covered, sum(1 for r in rows if r["due"])


def render(rows: list[dict], day: str, expected: int, covered: int) -> str:
    arrived = sum(1 for r in rows if r["status"].startswith(("✅", "⚠️")))
    missing = sum(1 for r in rows if r["status"].startswith("❌"))
    running = sum(1 for r in rows if r["status"].startswith("🔄"))
    lines = [
        f"# 档期对账 · {day}（北京时间）",
        "",
        f"- 当日应到：**{expected}** 个目标 · 可核对：**{covered}** 个",
        f"- 实到（推送成功）：**{arrived}** · 未到或未成功：**{missing}** · 运行中：**{running}**",
        f"- 未覆盖：**{sum(1 for r in rows if r['status'].startswith('⬜') and r['due'])}** 个档期"
        "（私有仓库缺 `AUDIT_PAT` 或 API 查询失败）—— **这些档期的漏跑本次根本不会被发现**",
        "- 档期表来源：`cloudflare/worker.js` 的 `PLAN`；公开仓库未配 `AUDIT_PAT` 时自动匿名查询",
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
        "❌ 应到且可核对，但当天没有成功推送（没触发，或触发了但全部失败/取消）；"
        "🔄 已触发但还没跑完，本次不判定；⏳ 计划时刻还没到，本次不判定。",
        "",
        "> 「❌ 触发但失败」表示**确实跑了但没推成功**——失败告警可能已经单独发过，"
        "这条对账是兜底，两者都看到才说明告警链路本身是通的。",
        "",
    ]
    return "\n".join(lines)


def default_audit_day() -> str:
    """返回「刚过去的那个完整日」（北京时间）。

    归档 cron 是北京 23:30，但本账号 GitHub schedule 实测延迟 3~10 小时。
    一旦凌晨才真正跑起来，datetime.now(BJ).date() 已经是新的一天，脚本会把
    前一天所有档期标成「⏳ 未到时刻」，前一天的漏跑就永远没人核对 ——
    监控任务自己被调度延迟打瞎。因此中午 12 点之前一律回看前一天。
    手动 --date / AUDIT_DATE 优先，便于补对历史日期。
    """
    now = datetime.now(BJ)
    if now.hour < 12:
        return (now.date() - timedelta(days=1)).isoformat()
    return now.date().isoformat()


def main() -> int:
    ap = argparse.ArgumentParser(description="当日档期应到/实到对账")
    ap.add_argument("--date", default=os.getenv("AUDIT_DATE", ""),
                    help="北京时间日期 YYYY-MM-DD，默认回看刚过去的完整日")
    args = ap.parse_args()
    day = args.date or default_audit_day()

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

    not_run = sum(1 for r in rows if r["status"] == "❌ 未到")
    failed = sum(1 for r in rows if r["status"] == "❌ 触发但失败")
    missing = not_run + failed
    running = sum(1 for r in rows if r["status"].startswith("🔄"))
    uncovered = sum(1 for r in rows if r["status"].startswith("⬜") and r["due"])
    if uncovered:
        print(f"::warning::{uncovered} 个当日档期未能核对（缺 AUDIT_PAT / 私有仓库无权限）。"
              "这部分档期即使漏跑也不会被本次对账发现，请补配 PAT 后重跑。")
    if not_run:
        print(f"::error::{not_run} 个当日应到档期没有任何触发记录")
    if failed:
        print(f"::error::{failed} 个当日档期触发了但全部失败/取消（当天没有成功推送）")
    if missing:
        return 1
    if running:
        print(f"::warning::{running} 个档期已触发但仍在运行，本次未判定，稍后可重跑对账")
    print("档期对账完成：已覆盖范围内全部推送成功")
    return 0


if __name__ == "__main__":
    sys.exit(main())
