#!/usr/bin/env python3
"""推送幂等守卫 · 基于仓库内台账（标准库实现，无第三方依赖）。

## 为什么改成台账

旧版靠「查 Actions API 里有没有 conclusion=success 的 run」来判断本档期是否推过。
但非交易日跳过、盘中迟到不推、preview/demo 调试、手动 --no-push 这些运行**也全是 success**，
会把当天真正要发的那条消息误判成「已推过」而跳过（静默漏推）。

现在改成：只有**飞书确实推送成功**之后，脚本才调用 mark_pushed() 往仓库里的
.push-state/ledger.json 写一条记录；should_skip() 只认这个台账。
GitHub cron 延迟到达时读台账发现本档已推 → 秒级退出，不会重复打扰飞书。

## 档期（edition）

- 单档任务（早间 / 晚间 / 收盘）：默认「当前时刻减 cutoff_hour 小时」落在哪天，就算哪档。
  默认 8 等效于「北京时间早 08:00 换日」：晚间 22:13 正常触发与次日凌晨 03:00 延迟触发、
  收盘 15:43 与次日 01:25 延迟触发，都会归到同一档。
- cutoff 怎么选：盘中 / 收盘可用 12（换日推迟到中午，覆盖更长延迟）；
  **早间 08:43 必须用 8（或 0），不能用 12** —— 08:43 减 12 小时会落到前一天，
  与中午的延迟档分家，反而造成重复推送。
- 双档任务（涨停接力 13:07 / 15:37 共用一个 workflow）：调用时传 slot="1307" / "1537"，
  档期键会变成 "日期-slot"，同一天的两个档互不干扰。

## 失败策略：一律 fail-open

台账读不到、写不了、token 缺失、API 抖动，都**不阻断推送**，也**不跳过推送**。
宁可偶尔重复一条，也不能因为守卫本身出问题导致当天没有消息。

## 环境变量

  GITHUB_TOKEN / GH_TOKEN   Actions 内置 token（workflow 需 contents: write）
  GITHUB_REPOSITORY         Actions 自动注入，形如 owner/repo
  GITHUB_RUN_ID             Actions 自动注入，仅用于台账留痕
  FORCE_PUSH=1              强制跳过检查（手动重推用）
  PUSH_GUARD_DISABLE=1      完全关闭守卫（应急）
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

BJ_TZ = timezone(timedelta(hours=8))
API = "https://api.github.com"
LEDGER_PATH = ".push-state/ledger.json"
DEFAULT_CUTOFF_HOUR = 8
MAX_RECORDS = 200

__all__ = [
    "should_skip", "mark_pushed", "edition_date", "edition_key",
    "already_pushed", "DEFAULT_CUTOFF_HOUR",
]


def edition_date(moment: datetime | None = None, cutoff_hour: int = DEFAULT_CUTOFF_HOUR) -> date:
    """把实际运行时刻归到它所属的推送档期。"""
    moment = moment or datetime.now(BJ_TZ)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=BJ_TZ)
    return (moment.astimezone(BJ_TZ) - timedelta(hours=cutoff_hour)).date()


def edition_key(workflow_file: str = "", slot: str | None = None,
                cutoff_hour: int = DEFAULT_CUTOFF_HOUR,
                now: datetime | None = None) -> str:
    """档期键：日期，或 日期-slot（同一天多档的任务）。"""
    key = edition_date(now, cutoff_hour).isoformat()
    if slot:
        key = f"{key}-{slot}"
    return key


def _token() -> str:
    return (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()


def _repo() -> str:
    return (os.environ.get("GITHUB_REPOSITORY") or "").strip()


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "push-guard",
        "Content-Type": "application/json",
    }


def _request(method: str, url: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except Exception as exc:  # noqa: BLE001 - 网络抖动一律 fail-open
        print(f"[幂等] API 异常 {type(exc).__name__}: {exc}")
        return 0, {}


def _read_ledger() -> tuple[list, str | None, bool]:
    """返回 (records, sha, available)。available=False 表示无法确认，调用方应 fail-open。"""
    if not _token() or not _repo():
        return [], None, False
    url = f"{API}/repos/{_repo()}/contents/{LEDGER_PATH}?ref=main"
    code, data = _request("GET", url)
    if code == 404:
        return [], None, True          # 台账还没建，第一次运行
    if code != 200:
        print(f"[幂等] 读取台账失败 HTTP {code}，按未推送处理")
        return [], None, False
    try:
        raw = base64.b64decode(data.get("content", "")).decode("utf-8")
        records = (json.loads(raw) or {}).get("records") or []
        return records, data.get("sha"), True
    except Exception as exc:  # noqa: BLE001
        print(f"[幂等] 台账解析失败 {type(exc).__name__}，按未推送处理")
        return [], None, False


def _write_ledger(records: list, sha: str | None) -> bool:
    if not _token() or not _repo():
        return False
    body = {
        "version": 1,
        "updated_at": datetime.now(BJ_TZ).isoformat(timespec="seconds"),
        "records": records[-MAX_RECORDS:],
    }
    payload = {
        "message": "chore(state): 更新推送台账 [skip ci]",
        "content": base64.b64encode(
            json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        ).decode("ascii"),
        "branch": "main",
    }
    if sha:
        payload["sha"] = sha
    code, _ = _request("PUT", f"{API}/repos/{_repo()}/contents/{LEDGER_PATH}", payload)
    if code in (200, 201):
        return True
    print(f"[幂等] 写入台账失败 HTTP {code}（不影响本次推送）")
    return False


def _forced() -> bool:
    return os.environ.get("FORCE_PUSH", "").strip().lower() in ("1", "true", "yes")


def already_pushed(workflow_file: str, edition: str,
                   cutoff_hour: int = DEFAULT_CUTOFF_HOUR) -> tuple[bool, str]:
    """兼容旧接口：查台账里本档期是否已推送成功。"""
    records, _, available = _read_ledger()
    if not available:
        return False, "台账不可用，按未推送处理"
    for rec in records:
        if rec.get("workflow") == workflow_file and rec.get("edition") == str(edition):
            return True, f"档期 {edition} 已在 {rec.get('ts', '?')} 推送成功（run {rec.get('run_id', '?')}）"
    return False, f"档期 {edition} 尚无成功推送记录"


def should_skip(workflow_file: str, force: bool = False,
                now: datetime | None = None,
                cutoff_hour: int = DEFAULT_CUTOFF_HOUR,
                slot: str | None = None) -> bool:
    """主入口：True 表示本档期已经推过，直接退出，不要再发消息。"""
    if os.environ.get("PUSH_GUARD_DISABLE", "").strip() == "1":
        print("[幂等] PUSH_GUARD_DISABLE=1，守卫已关闭")
        return False
    if force or _forced():
        print("[幂等] FORCE 已开启，跳过检查，强制推送")
        return False

    key = edition_key(workflow_file, slot, cutoff_hour, now)
    records, _, available = _read_ledger()
    if not available:
        print("[幂等] 台账不可用，fail-open：照常推送（可能重复，但不漏推）")
        return False
    for rec in records:
        if rec.get("workflow") == workflow_file and rec.get("edition") == key:
            print(f"⏭ 跳过本次推送：档期 {key} 已在 {rec.get('ts', '?')} 推送成功"
                  f"（run {rec.get('run_id', '?')}）")
            print("   （外部准点触发 + GitHub cron 延迟触发时，只有先到的那次真的推；"
                  "要强制重推请设 FORCE_PUSH=1）")
            return True
    print(f"[幂等] 档期 {key} 尚无成功推送记录，继续推送")
    return False


def mark_pushed(workflow_file: str,
                cutoff_hour: int = DEFAULT_CUTOFF_HOUR,
                slot: str | None = None) -> bool:
    """**只在飞书确实推送成功之后**调用。写入台账，供后续重复触发判重。"""
    if os.environ.get("PUSH_GUARD_DISABLE", "").strip() == "1":
        return False
    if not _token() or not _repo():
        print("[幂等] 缺少 GITHUB_TOKEN / GITHUB_REPOSITORY，台账未写入（不影响本次推送）")
        return False

    key = edition_key(workflow_file, slot, cutoff_hour)
    record = {
        "workflow": workflow_file,
        "edition": key,
        "run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "ts": datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S"),
    }
    # 同一档期多个 workflow 可能并发写，用 sha 冲突重试 + 重新合并
    for attempt in range(3):
        records, sha, available = _read_ledger()
        if not available:
            return False
        if any(r.get("workflow") == workflow_file and r.get("edition") == key for r in records):
            print(f"[幂等] 档期 {key} 已有记录，无需重复写入")
            return True
        if _write_ledger(records + [record], sha):
            print(f"[幂等] 已记录推送成功：{key}")
            return True
        if attempt < 2:
            time.sleep(0.6 * (attempt + 1))
    print("[幂等] 台账写入重试 3 次仍失败（不影响本次推送）")
    return False


def _main(argv: list[str]) -> int:
    workflow = "evening-news.yml"
    for arg in argv[1:]:
        if not arg.startswith("-"):
            workflow = arg
    force = "--force" in argv
    slot = next((a.split("=", 1)[1] for a in argv if a.startswith("--slot=")), None)
    cutoff = next((int(a.split("=", 1)[1]) for a in argv if a.startswith("--cutoff=")), DEFAULT_CUTOFF_HOUR)
    skip = should_skip(workflow, force=force, cutoff_hour=cutoff, slot=slot)
    print(f"workflow={workflow} edition={edition_key(workflow, slot, cutoff)} "
          f"→ {'SKIP' if skip else 'PUSH'}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
