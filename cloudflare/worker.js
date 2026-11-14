/**
 * 统一准点调度器 · 一个 Worker 接管全部 GitHub Actions 定时任务
 *
 * 背景
 * ----
 * GitHub Actions 的 `schedule` 只保证「尽力而为」：Wind866 账号实测 34 次定时运行
 * 0 次准时，延迟 3.3~9.7 小时，且高负载时排队任务会被直接丢弃（官方文档明确写明）。
 * 因此把准点触发交给外部定时器，仓库里的 cron 只保留作兜底。
 *
 * 为什么用「一个每分钟 cron + 内部时间表」
 * ------------------------------------
 * Cloudflare Workers 免费版：**整个账号只有 5 个 Cron Trigger**（不是每个 Worker 5 个）。
 * 这里共 9 个推送档期，逐个配 cron 装不下。改用单个 `* * * * *` 每分钟唤醒一次，
 * 在代码里换算成北京时间并查表命中，只占 1 个额度；每天 1440 次调用，
 * 远低于免费版 100,000 次/天。未命中的分钟不做任何网络请求。
 *
 * 需要配置的 Secrets / Variables
 * ------------------------------
 *   GH_TOKEN        Secret  fine-grained PAT，勾选下面 3 个仓库的 Actions: Read and write
 *                           BIGApush / limit-up-relay / etf-feishu-push
 *   ALERT_WEBHOOK   Secret  独立的飞书机器人 webhook，用于调度失败告警
 *                           （不要和业务推送共用同一个，否则 webhook 挂了告警也发不出去）
 *   OWNER           Text    Wind866
 *   BRANCH          Text    main
 *
 * 部署前必读
 * ----------
 * 1. 如果之前部署过旧的 biga-push-cron（晚间新闻专用），请先删掉它的 cron 或整个 Worker，
 *    否则 22:13 会被两个 Worker 各触发一次。
 * 2. 本 Worker 只负责「准点触发」，不保证「不重复推送」。重复触发的去重由各仓库脚本侧的
 *    幂等守卫负责（BIGApush 已有 utils/push_guard.py，其余仓库需同步接入）。
 * 3. 仓库侧需要同步改动：给所有 workflow 加 actions: read 权限、给推送脚本接幂等守卫、
 *    给 limit-up-relay 的 dispatch 触发补档期推断。详见体检报告的「仓库侧改动清单」。
 */

const API = "https://api.github.com";

// 每次改动 worker.js 都递增这个标识。/health 会把它暴露出来，
// 用于核对「Cloudflare 线上跑的是不是仓库里这一版」——此前线上版本与仓库
// 不同步（缺 /test 路由、档期名不一致），靠肉眼看代码无法发现。
const BUILD = "2026-11-14.1";

// 档期表。at 是北京时间 HH:MM；days: all / weekday / 1(Mon) / 2(Tue)
// weekday 只避开周末，A 股节假日由各脚本自己的交易日探测跳过。
const PLAN = [
  {
    at: "08:43", days: "all", name: "早间新闻",
    targets: ["BIGApush/morning-news.yml"],
  },
  {
    at: "13:07", days: "weekday", name: "涨停接力 · 盘中快照",
    targets: ["limit-up-relay/daily.yml"],
  },
  {
    at: "13:13", days: "weekday", name: "盘中选股",
    targets: ["BIGApush/intraday-selection.yml"],
  },
  {
    at: "15:37", days: "weekday", name: "涨停接力 · 收盘定稿",
    targets: ["limit-up-relay/daily.yml"],
  },
  {
    at: "15:43", days: "weekday", name: "收盘选股",
    targets: ["BIGApush/selection.yml"],
  },
  {
    at: "20:00", days: "weekday", name: "ETF 资金流向",
    targets: ["etf-feishu-push/daily-push.yml"],
  },
  {
    at: "22:13", days: "all", name: "晚间新闻",
    targets: ["BIGApush/evening-news.yml"],
  },
  {
    at: "09:00", days: 1, name: "数据源健康自检（周一）",
    targets: ["BIGApush/check-sources.yml", "limit-up-relay/check-sources.yml"],
  },
  {
    at: "01:00", days: 2, name: "Keep Alive（周二）",
    targets: ["BIGApush/keep-alive.yml"],
  },
];

const GH_HEADERS = (token) => ({
  Authorization: `Bearer ${token}`,
  Accept: "application/vnd.github+json",
  "X-GitHub-Api-Version": "2022-11-28",
  "User-Agent": "gh-actions-scheduler",
});

function beijingNow(nowMs) {
  // 加 8 小时后用 UTC getter 读出，等价于北京时间字段，且不受 Worker 所在时区影响
  const bj = new Date(nowMs + 8 * 3600 * 1000);
  const hh = String(bj.getUTCHours()).padStart(2, "0");
  const mm = String(bj.getUTCMinutes()).padStart(2, "0");
  return {
    hhmm: `${hh}:${mm}`,
    date: `${bj.getUTCFullYear()}-${String(bj.getUTCMonth() + 1).padStart(2, "0")}-${String(bj.getUTCDate()).padStart(2, "0")}`,
    dow: bj.getUTCDay(), // 0=周日 1=周一 ... 6=周六
  };
}

function matches(slot, bj) {
  if (slot.days === "all") return true;
  if (slot.days === "weekday") return bj.dow >= 1 && bj.dow <= 5;
  return bj.dow === slot.days;
}

async function notify(env, text) {
  const hook = (env.ALERT_WEBHOOK || "").trim();
  console.log(`[ALERT] ${text}`);
  if (!hook) return; // 没配独立告警通道时至少留在 Worker 日志里
  try {
    await fetch(hook, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ msg_type: "text", content: { text: `⚠️ 定时调度异常\n${text}` } }),
    });
  } catch (err) {
    console.error(`[ALERT] 告警通道也失败了: ${err}`);
  }
}

async function authHeaders(env) {
  const token = (env.GH_TOKEN || "").trim();
  if (!token) throw new Error("未配置 GH_TOKEN");
  return GH_HEADERS(token);
}

/** 3 分钟内本 Worker 已经 dispatch 过同一个 workflow 就跳过，防止 CF 重复唤醒 */
async function recentlyDispatched(env, headers, owner, target) {
  const [repo, workflow] = target.split("/");
  try {
    const res = await fetch(
      `${API}/repos/${owner}/${repo}/actions/workflows/${workflow}/runs?per_page=5`,
      { headers },
    );
    if (!res.ok) return false; // 查不到就照常触发，宁可重复也不要漏
    const data = await res.json();
    const cutoff = Date.now() - 3 * 60 * 1000;
    return (data.workflow_runs || []).some(
      (r) => r.event === "workflow_dispatch" && new Date(r.created_at).getTime() > cutoff,
    );
  } catch {
    return false;
  }
}

async function dispatchOne(env, headers, owner, branch, target, slot) {
  const [repo, workflow] = target.split("/");
  const url = `${API}/repos/${owner}/${repo}/actions/workflows/${workflow}/dispatches`;
  let last = "";

  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: { ...headers, "content-type": "application/json" },
        // 不传 inputs：未在 workflow 里声明的 input 会被 API 拒绝，
        // 各 workflow 的默认值已经足够（normal / close / main）。
        body: JSON.stringify({ ref: branch }),
      });
      if (res.status === 204) {
        return `✅ ${target} (${slot.name}) 已触发`;
      }
      const body = (await res.text()).slice(0, 300);
      last = `❌ ${target} HTTP ${res.status} ${body}`;
      // 401/403 = token 过期或权限不足，404 = 仓库名/文件名错，重试没有意义
      if (res.status < 500) break;
    } catch (err) {
      last = `❌ ${target} 请求异常 ${err}`;
    }
    if (attempt < 3) await new Promise((r) => setTimeout(r, 800 * attempt));
  }
  return last;
}

async function runSlot(env, slot, bj) {
  const owner = (env.OWNER || "Wind866").trim();
  const branch = (env.BRANCH || "main").trim();
  let headers;
  try {
    headers = await authHeaders(env);
  } catch (err) {
    await notify(env, `${bj.date} ${slot.at} ${slot.name}：${err}`);
    throw err;
  }

  const lines = [];
  let failed = 0;
  for (const target of slot.targets) {
    if (await recentlyDispatched(env, headers, owner, target)) {
      lines.push(`⏭ ${target} 3 分钟内已触发，跳过`);
      continue;
    }
    const out = await dispatchOne(env, headers, owner, branch, target, slot);
    lines.push(out);
    if (out.startsWith("❌")) failed++;
  }

  const text = `[${bj.date} ${slot.at}] ${slot.name}\n${lines.join("\n")}`;
  console.log(text);
  if (failed) {
    await notify(env, `${bj.date} ${slot.at} ${slot.name} 有 ${failed} 个触发失败：\n${lines.join("\n")}`);
    throw new Error(text); // 让 CF 调用记录标红
  }
  return text;
}

/** 该档期在计划时刻 ±10 分钟内是否已经留下过 dispatch 记录。
 *  查不到 API 时一律返回 true（当作已触发），宁可漏补也不重复推送。 */
async function firedNearSlot(env, headers, owner, target, slot, bj) {
  const [repo, workflow] = target.split("/");
  const planned = Date.parse(`${bj.date}T${slot.at}:00+08:00`);
  if (Number.isNaN(planned)) return true;
  try {
    const res = await fetch(
      `${API}/repos/${owner}/${repo}/actions/workflows/${workflow}/runs?per_page=20`,
      { headers },
    );
    if (!res.ok) return true;
    const data = await res.json();
    return (data.workflow_runs || []).some(
      (r) => r.event === "workflow_dispatch" &&
        Math.abs(new Date(r.created_at).getTime() - planned) <= 10 * 60 * 1000,
    );
  } catch {
    return true;
  }
}

/** 补档：CF 偶发丢弃某次唤醒时，每 5 分钟回看刚过去 2~7 分钟的档期并补触发。
 *  不依赖 KV / D1，判重直接查 Actions API；正常触发过的档期不会重复发。 */
async function catchUp(env, bj) {
  let headers;
  try {
    headers = await authHeaders(env);
  } catch {
    return;
  }
  const owner = (env.OWNER || "Wind866").trim();
  const branch = (env.BRANCH || "main").trim();
  const nowMin = Number(bj.hhmm.slice(0, 2)) * 60 + Number(bj.hhmm.slice(3));

  for (const slot of PLAN) {
    if (!matches(slot, bj)) continue;
    const [h, m] = slot.at.split(":").map(Number);
    const age = nowMin - (h * 60 + m);
    if (age < 2 || age > 7) continue;
    for (const target of slot.targets) {
      if (await firedNearSlot(env, headers, owner, target, slot, bj)) continue;
      const out = await dispatchOne(env, headers, owner, branch, target, slot);
      console.log(`[CATCH-UP] ${bj.date} ${slot.at} ${target} → ${out}`);
      if (out.startsWith("❌")) {
        await notify(env, `${bj.date} ${slot.at} ${slot.name} 补档失败：${out}`);
      }
    }
  }
}

/** 每天 08:00 主动验一次 token，避免 PAT 过期后「静默不推」好几天才发现 */
async function tokenSelfCheck(env) {
  try {
    const headers = await authHeaders(env);
    const res = await fetch(`${API}/user`, { headers });
    if (res.ok) {
      console.log("[SELF-CHECK] GH_TOKEN 有效");
      return;
    }
    const exp = res.headers.get("github-authentication-token-expiration");
    await notify(
      env,
      `GH_TOKEN 自检失败 HTTP ${res.status}${exp ? `（过期时间 ${exp}）` : ""}。` +
        `请到 GitHub Settings → Developer settings 轮换 PAT，并用 wrangler secret put GH_TOKEN 更新。`,
    );
  } catch (err) {
    await notify(env, `GH_TOKEN 自检异常：${err}`);
  }
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj, null, 2) + "\n", {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

export default {
  async scheduled(event, env, ctx) {
    // 必须用 cron 的计划时刻，不能用 Date.now()：Worker 冷启动 / CF 调度抖动
    // 会让实际执行落进下一分钟，而档期是精确到 HH:MM 的字符串比对，
    // 一跨分钟就永久错过该档期（08:43 档期无运行记录的根因）。
    const plannedMs = Number(event.scheduledTime) || Date.now();
    const bj = beijingNow(plannedMs);

    const driftSec = Math.round(Math.abs(Date.now() - plannedMs) / 1000);
    if (driftSec > 120) {
      ctx.waitUntil(notify(env,
        `CF cron 执行偏差 ${driftSec}s（计划 ${bj.date} ${bj.hhmm}）；已按计划时刻匹配档期`));
    }

    // 每日令牌自检（不占用 cron 额度，就是每分钟唤醒里的一分钟）
    if (bj.hhmm === "08:00") {
      ctx.waitUntil(tokenSelfCheck(env));
    }

    const hit = PLAN.find((s) => s.at === bj.hhmm && matches(s, bj));
    if (hit) {
      ctx.waitUntil(runSlot(env, hit, bj));
      return;
    }

    // 每 5 分钟做一次补档回看。绝大多数分钟仍然零网络请求。
    if (Number(bj.hhmm.slice(3)) % 5 === 0) {
      ctx.waitUntil(catchUp(env, bj));
    }
  },

  async fetch(req, env) {
    const url = new URL(req.url);

    if (url.pathname === "/health") {
      let tokenValid = null;
      if (url.searchParams.get("check") === "1") {
        try {
          const res = await fetch(`${API}/user`, { headers: await authHeaders(env) });
          tokenValid = res.ok;
        } catch {
          tokenValid = false;
        }
      }
      return json({
        ok: true,
        build: BUILD,
        cronExpected: "* * * * *",
        owner: (env.OWNER || "Wind866").trim(),
        branch: (env.BRANCH || "main").trim(),
        slots: PLAN.length,
        targets: PLAN.flatMap((s) => s.targets).length,
        schedule: PLAN.map((s) => `${s.at} ${s.name} [${s.targets.length}]`),
        tokenValid,
        alertConfigured: Boolean((env.ALERT_WEBHOOK || "").trim()),
        note: "根路径不会触发任务；/test?at=HH:MM 手动测某档期，/health?check=1 校验 token；build 字段用于核对线上是否为此版代码",
      });
    }

    // 手动测试指定档期，例如 /test?at=22:13；不会像旧版那样一打开就全量触发
    if (url.pathname === "/test") {
      const at = url.searchParams.get("at") || "";
      const slot = PLAN.find((s) => s.at === at);
      if (!slot) return json({ ok: false, error: `未知档期 ${at}`, available: PLAN.map((s) => s.at) }, 400);
      try {
        const text = await runSlot(env, slot, beijingNow(Date.now()));
        return new Response(text + "\n", { headers: { "content-type": "text/plain; charset=utf-8" } });
      } catch (err) {
        return new Response(String(err) + "\n", { status: 500, headers: { "content-type": "text/plain; charset=utf-8" } });
      }
    }

    return new Response(
      "GitHub Actions 统一准点调度器\n\n/health            查看档期表与配置状态\n/health?check=1     校验 GH_TOKEN\n/test?at=22:13     手动触发某个档期（自测用）\n",
      { headers: { "content-type": "text/plain; charset=utf-8" } },
    );
  },
};
