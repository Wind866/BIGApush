#!/usr/bin/env python3
"""推送幂等守卫：保证同一「档期」只成功推送一次。

## 为什么需要它

GitHub 的 schedule 派发不可靠（本仓库实测：33 次定时运行 0 次准时，
延迟 3h17m ~ 9h43m）。解决办法是在外部定时器（Cloudflare Worker）里
准点 workflow_dispatch 一次，但这样就变成「两个触发器打同一个 workflow」，
当 GitHub 那条延迟到凌晨 3 点时，同一档新闻会被推第二次。

于是每次推送前先反查 Actions API：本档期是否已有成功运行？有就跳过。

## 档期（edition）怎么算

不能直接用日历日：晚间新闻 22:13 的正常运行在「今天」，
而延迟到凌晨 3 点的那次在「明天」，按日历日判断两个都算不同档，会重复推送。

所以引入 cutoff：**当前时间减 8 小时后落在哪一天，就算哪一档**。

    22:13 触发 → 减 8h = 14:13 → 当天      ✓
    03:00 触发 → 减 8h = 19:00 → 昨天      ✓ 归到同一档
    08:00 触发 → 减 8h = 00:00 → 当天      （边界，早间新闻用 8 点前归前一天）

早间新闻若要用，把 cutoff_hour 设成 12 即可（08:43 不减，13:00 延迟也还是当天）。

## 失败时的行为

任何异常都**不阻断推送**（fail-open）：查不到就照常推。
宁可偶尔重复一条，也不能因为 API 抖动当天没有消息。
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone

BJ_TZ = timezone(timedelta(hours=8))
DEFAULT_CUTOFF_HOUR = 8
API = "https://api.github.com"


def edition_date(moment: datetime, cutoff_hour: int = DEFAULT_CUTOFF_HOUR) -> date:
    """把「实际时刻」归到它所属的推送档期。"""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=BJ_TZ)
    return (moment.astimezone(BJ_TZ) - timedelta(hours=cutoff_hour)).date()


def _parse_ts(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def already_pushed(workflow_file: str,
                   edition: date,
                   exclude_run_id: str | None = None,
                   cutoff_hour: int = DEFAULT_CUTOFF_HOUR,
                   timeout: int = 15) -> tuple[bool, str]:
    """本档期是否已成功推送过。返回 (是否已推, 说明)。"""
    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    repo = (os.environ.get("GITHUB_REPOSITORY") or "").strip()
    if not token or not repo:
        return False, "缺少 GITHUB_TOKEN / GITHUB_REPOSITORY，跳过幂等检查"

    try:
        import requests
    except ImportError:
        return False, "requests 未安装，跳过幂等检查"

    params = {
        "created": f">={edition - timedelta(days=1):%Y-%m-%d}",
        "per_page": 50,
    }
    try:
        resp = requests.get(
            f"{API}/repos/{repo}/actions/workflows/{workflow_file}/runs",
            params=params,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=timeout,
        )
        if resp.status_code != 200:
            return False, f"查询运行记录失败 HTTP {resp.status_code}，按未推送处理"
        runs = (resp.json() or {}).get("workflow_runs") or []
    except Exception as exc:  # noqa: BLE001 - 网络抖动不该拦住推送
        return False, f"查询运行记录异常 {type(exc).__name__}: {exc}，按未推送处理"

    for run in runs:
        if str(run.get("id")) == str(exclude_run_id):
            continue
        if run.get("conclusion") != "success":
            continue
        created = _parse_ts(run.get("created_at"))
        if not created:
            continue
        if edition_date(created, cutoff_hour) != edition:
            continue
        when = created.astimezone(BJ_TZ)
        return True, (f"档期 {edition} 已在 {when:%m-%d %H:%M} 成功推送过"
                      f"（run {run.get('id')}，触发方式 {run.get('event')}）")
    return False, f"档期 {edition} 尚无成功推送"


def should_skip(workflow_file: str,
                force: bool = False,
                now: datetime | None = None,
                cutoff_hour: int = DEFAULT_CUTOFF_HOUR) -> bool:
    """主入口：True 表示今天这档已经推过，直接退出。"""
    if force:
        print("[幂等] FORCE 已开启，跳过检查，强制推送")
        return False

    now = now or datetime.now(BJ_TZ)
    edition = edition_date(now, cutoff_hour)
    done, reason = already_pushed(
        workflow_file,
        edition,
        exclude_run_id=os.environ.get("GITHUB_RUN_ID"),
        cutoff_hour=cutoff_hour,
    )
    if done:
        print(f"⏭ 跳过本次推送：{reason}")
        print("   （外部定时器与 GitHub 自带 cron 都触发时，只有先到的那个会真的推送；"
              "要强制重推请设 FORCE_PUSH=1）")
        return True
    print(f"[幂等] {reason}，继续推送")
    return False


def _main(argv: list[str]) -> int:
    workflow = argv[1] if len(argv) > 1 else "evening-news.yml"
    force = "--force" in argv
    cutoff = DEFAULT_CUTOFF_HOUR
    for arg in argv:
        if arg.startswith("--cutoff="):
            cutoff = int(arg.split("=", 1)[1])
    now = None
    for arg in argv:
        if arg.startswith("--now="):
            now = datetime.fromisoformat(arg.split("=", 1)[1]).replace(tzinfo=BJ_TZ)
    skip = should_skip(workflow, force=force, now=now, cutoff_hour=cutoff)
    print(f"workflow={workflow} cutoff={cutoff} → {'SKIP' if skip else 'PUSH'}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
