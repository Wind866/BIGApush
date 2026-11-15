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

优先用 **slot_hour（目标档期的小时）** 归并，而不是固定 cutoff：

    edition = (实际运行时刻 − slot_hour 小时).date()

- 晚间 22:13 档，slot_hour=22：22:13 跑 → 归当天；次日 05:00 延迟跑 →
  05:00−22h 落到前一天 07:00 → 仍归前一天；次日 11:20 晚跑 → 也归前一天。
- 收盘 15:43 档，slot_hour=15；盘中 13:13 档，slot_hour=13；早间 08:43 档，slot_hour=8。
- 这样"前一天档期迟到十几小时"不会再去抢今天的档期。

## 为什么不再用固定 cutoff（历史坑）

旧实现用 `cutoff_hour=8` 表示"早 08:00 换日"。晚间 22:13 档遇到 GitHub
7~9 小时的调度延迟、甚至次日 11:20 才跑到时，`11:20 − 8h = 03:20` 落在**当天**，
于是把"昨天晚上的新闻"记成了"今天晚上的档期已推送"，当晚 22:13 的推送被
`should_skip()` 直接跳过——静默漏推，而且账本上看不出哪里错了。

slot_hour 仍然向后兼容：不传时退回 cutoff_hour 行为。

## 失败策略：一律 fail-open

台账读不到、写不了、token 缺失、API 抖动，都**不阻断推送**，也**不跳过推送**。
宁可偶尔重复一条，也不能因为守卫本身出问题导致当天没有消息。

## 为什么这里仍然走 Contents API（与 limit-up-relay 不同）

limit-up-relay / etf 已经改成「本地文件台账 + 与数据快照同一次 commit」，因为
那边每天都要提交 docs/data、data/snapshots，Contents API 抢先产生 commit 会让
紧接着的 ``git push`` 变成 non-fast-forward，进而把 Pages 部署整段 skip 掉。

本仓库没有「每天提交数据」的步骤：快照只留在 runner 上、不入库，唯一的写入方
是每周一次的 keep-alive，而它本身就带 ``git pull --rebase`` 重试，所以那条
non-fast-forward 路径在这里不会发生。改成本地台账反而要求四个工作流各加一段
提交步骤（漏一个就退化成「每天重复推送」），因此这里保持 API 台账，
只在真出现「数据提交」需求时再一并迁移。

## 环境变量

  GITHUB_TOKEN / GH_TOKEN   Actions 内置 token（workflow 需 contents: write）
  GITHUB_REPOSITORY         Actions 自动注入，形如 owner/repo
  GITHUB_RUN_ID             Actions 自动注入，仅用于台账留痕
  FORCE_PUSH=1              强制跳过检查（手动重推用）
  PUSH_GUARD_DATE           跨夜补跑时显式指定「数据日期」YYYY-MM-DD（档期按它算）
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

def _edition_override() -> date | None:
    """PUSH_GUARD_DATE=YYYY-MM-DD：用「数据日期」而不是「运行时刻」归档期。

    收盘定稿档 15:43 被 GitHub 拖到次日凌晨才起来时，数据仍是上一交易日的。
    按运行时刻算档期会落到新的一天，和当天真正推过的记录对不上 → 同一份
    数据被重复推送一条。跨夜补跑由 workflow 把行情日期传进来即可对齐。
    """
    raw = (os.environ.get("PUSH_GUARD_DATE") or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        print(f"[幂等] PUSH_GUARD_DATE 格式非法（{raw}），已忽略，按运行时刻归档期")
        return None


def edition_date(moment: datetime | None = None, cutoff_hour: int = DEFAULT_CUTOFF_HOUR,
                 slot_hour: int | None = None) -> date:
    """把实际运行时刻归到它所属的推送档期。

    slot_hour 给了就按「目标档期小时」归并（推荐）：运行时刻 − slot_hour 小时。
    否则退回旧的「− cutoff_hour」行为。PUSH_GUARD_DATE 显式指定时以它为准。
    """
    override = _edition_override()
    if override is not None:
        return override
    moment = moment or datetime.now(BJ_TZ)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=BJ_TZ)
    shifted = moment.astimezone(BJ_TZ) - timedelta(hours=slot_hour if slot_hour is not None else cutoff_hour)
    return shifted.date()


def edition_key(workflow_file: str = "", slot: str | None = None,
                cutoff_hour: int = DEFAULT_CUTOFF_HOUR,
                now: datetime | None = None,
                slot_hour: int | None = None) -> str:
    """档期键：日期，或 日期-slot（同一天多档的任务）。"""
    key = edition_date(now, cutoff_hour, slot_hour).isoformat()
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


def _record_matches(rec: dict, workflow_file: str, key: str,
                    cutoff_hour: int, slot_hour: int | None) -> bool:
    """台账里这条记录是否真的代表「key 这一档已推送」。

    只比对 edition 字段不够：旧逻辑（固定 cutoff）会把「昨天 22:13 的档期，
    今天 11:20 才跑到」记成 `2026-11-14`，这条脏记录会让今晚真正的 22:13
    被误判为已推送 —— 也就是修复上线后仍会漏推一次。

    所以这里用记录自己的时间戳按**当前口径**再算一遍档期：
    算出来与 key 不一致，说明它记的是别的档期，不认账（宁可多推一条）。
    时间戳解析不了就照旧认账，避免因为格式问题变成天天重推。
    """
    if rec.get("workflow") != workflow_file or rec.get("edition") != key:
        return False
    raw = str(rec.get("ts") or "").strip()
    if not raw:
        return True
    try:
        moment = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BJ_TZ)
    except ValueError:
        return True
    try:
        actual = edition_date(moment, cutoff_hour, slot_hour).isoformat()
    except Exception:  # noqa: BLE001
        return True

    # key 可能是 "2026-11-14" 或 "2026-11-14-1307"（同一天多档），只比日期部分
    expected_date = "-".join(key.split("-")[:3])
    if actual != expected_date:
        print(f"[幂等] 忽略一条档期记错的旧记录：edition={key}，但它的时间戳 {raw} "
              f"按当前口径属于 {actual}（旧版固定 cutoff 会写错跨日档期）")
        return False
    return True


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
                slot: str | None = None,
                slot_hour: int | None = None) -> bool:
    """主入口：True 表示本档期已经推过，直接退出，不要再发消息。

    slot_hour 传目标档期的整点小时（晚间 22、收盘 15、盘中 13、早间 8），
    迟到十几小时的运行才不会被算成「今天的档期已推」。
    """
    if os.environ.get("PUSH_GUARD_DISABLE", "").strip() == "1":
        print("[幂等] PUSH_GUARD_DISABLE=1，守卫已关闭")
        return False
    if force or _forced():
        print("[幂等] FORCE 已开启，跳过检查，强制推送")
        return False

    key = edition_key(workflow_file, slot, cutoff_hour, now, slot_hour)
    records, _, available = _read_ledger()
    if not available:
        print("[幂等] 台账不可用，fail-open：照常推送（可能重复，但不漏推）")
        return False
    for rec in records:
        if _record_matches(rec, workflow_file, key, cutoff_hour, slot_hour):
            print(f"⏭ 跳过本次推送：档期 {key} 已在 {rec.get('ts', '?')} 推送成功"
                  f"（run {rec.get('run_id', '?')}）")
            print("   （外部准点触发 + GitHub cron 延迟触发时，只有先到的那次真的推；"
                  "要强制重推请设 FORCE_PUSH=1）")
            return True
    print(f"[幂等] 档期 {key} 尚无成功推送记录，继续推送")
    return False


def mark_pushed(workflow_file: str,
                cutoff_hour: int = DEFAULT_CUTOFF_HOUR,
                slot: str | None = None,
                slot_hour: int | None = None) -> bool:
    """**只在飞书确实推送成功之后**调用。写入台账，供后续重复触发判重。"""
    if os.environ.get("PUSH_GUARD_DISABLE", "").strip() == "1":
        return False
    if not _token() or not _repo():
        print("[幂等] 缺少 GITHUB_TOKEN / GITHUB_REPOSITORY，台账未写入（不影响本次推送）")
        return False

    key = edition_key(workflow_file, slot, cutoff_hour, None, slot_hour)
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
        # 注意：这里只认「真正匹配」的记录。旧逻辑写错的跨日脏记录不算，
        # 否则本档期的正确记录永远不会被补上。
        if any(_record_matches(r, workflow_file, key, cutoff_hour, slot_hour) for r in records):
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
    slot_hour = next((int(a.split("=", 1)[1]) for a in argv if a.startswith("--slot-hour=")), None)
    skip = should_skip(workflow, force=force, cutoff_hour=cutoff, slot=slot, slot_hour=slot_hour)
    print(f"workflow={workflow} edition={edition_key(workflow, slot, cutoff, None, slot_hour)} "
          f"→ {'SKIP' if skip else 'PUSH'}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
