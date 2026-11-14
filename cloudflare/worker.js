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
 *   FEISHU_APP_ID      Secret  自建应用 App ID（cli_ 开头），用于「群里发代码查个股」
 *   FEISHU_APP_SECRET  Secret  自建应用 App Secret
 *   FEISHU_ENCRYPT_KEY Secret  可选，飞书后台开了加密策略时才需要（仅事件回调模式用）
 *   FEISHU_CHAT_ID     Text    可选，指定要监听的群 id；不配则自动列出机器人所在的所有群
 *   OWNER           Text    Wind866
 *   BRANCH          Text    main
 *
 * 路由
 * ----
 *   GET  /            说明
 *   GET  /health      档期表与配置状态（?check=1 顺带验 GH_TOKEN）
 *   GET  /test?at=HH:MM  手动触发某档期
 *   GET  /feishu/poll 立刻轮询一次群消息（群里 @机器人 查个股，主用这条）
 *   GET  /feishu/selftest 通道自检（?send=1 真往群里发一条，用来验发言权限）
 *   POST /feishu      飞书事件回调（仅在拥有公网域名时启用；workers.dev 国内不可达）
 *
 * 群里查个股怎么工作
 * ----------------
 * 主路径是**轮询**：cron 每 2 分钟唤醒 → 调飞书「获取会话历史消息」→ 找 @了机器人
 * 且含 6 位数字的消息 → dispatch stock-query.yml。因为不依赖任何入站请求，
 * 所以不需要域名、不需要事件订阅，也不受飞书「3 秒超时」限制。代价是最多延迟 2 分钟。
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
const BUILD = "2026-11-14.10";

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

// ============================================================================
// 飞书机器人：群里发 6 位股票代码 → 触发 stock-query.yml → 结果推回群
// ============================================================================
//
// 为什么要自建应用，而不是沿用「自定义机器人 webhook」
// ------------------------------------------------
// 群自定义机器人（FEISHU_WEBHOOK）只能往外发，收不到群里说的话 —— 所以
// 「在飞书输入代码查股票」用 webhook 做不到。要双向只能走**自建应用**：
// 飞书把 im.message.receive_v1 事件 POST 到本 Worker 的 /feishu 路由，
// 这里解析代码 → dispatch GitHub Actions → 结论由 stock-query.yml 推回群。
//
// 需要配置的 Secrets（npx wrangler secret put 名称）
// ----------------------------------------------
//   FEISHU_APP_ID       自建应用 App ID（cli_ 开头）
//   FEISHU_APP_SECRET   自建应用 App Secret
//   FEISHU_ENCRYPT_KEY  可选。飞书后台开了「加密策略」才需要，不配则按明文解析
//
// 飞书开放平台侧（一次性）
// ----------------------
//   1. 权限：im:message（收发群/单聊消息）、im:message:send_as_bot
//   2. 事件订阅：im.message.receive_v1 → 请求地址 https://<本 Worker 域名>/feishu
//   3. 开启机器人能力，并把机器人拉进目标群
//   4. 若填了 Encrypt Key，事件会整包加密下发，本文件用 AES-256-CBC 自行解密

const FEISHU_API = "https://open.feishu.cn/open-apis";
const QUERY_TARGET = "BIGApush/stock-query.yml";
const MAX_QUERY_CODES = 6;
// 同一组代码 90 秒内只触发一次：飞书 3 秒没收到 200 会重投，连发同一条也常见
const CODE_COOLDOWN_MS = 90 * 1000;

// Worker 实例存活期内的事件去重表（CF 不保证同一实例处理重投，尽力而为）
const seenEvents = new Map();

// ---- 跨实例去重（Cache API）------------------------------------------------
// 为什么需要：cron 每分钟唤醒一次，但 CF 不保证复用同一个实例。新实例的
// pollSeen / seenEvents / notifyAt 全是空的，而冷启动回看 90 秒 > 轮询间隔
// 60 秒 —— 于是同一条消息会被「上一分钟的实例」和「这一分钟的实例」各处理
// 一遍，群里就出现两条「已收到」、Actions 也跑两遍（两次图片推送）。
// Cache 在同一 colo 内能跨实例存活，用它把「消息 id」和「代码」都记一笔。
const DEDUPE_TTL_SEC = 1800; // 30 分钟足够覆盖任何重试；过期自动失效，不需要清理

function dedupeUrl(key) {
  // Cache API 只认带 scheme 的 URL，内部不存在的域名不会真的发请求
  return `https://bigapush-dedupe.internal/${encodeURIComponent(key)}`;
}

async function dedupeGet(key) {
  if (typeof caches === "undefined" || !caches.default) return "";
  try {
    const hit = await caches.default.match(dedupeUrl(key));
    return hit ? (await hit.text()).trim() : "";
  } catch {
    return "";
  }
}

async function dedupeSet(key, value, ttl = DEDUPE_TTL_SEC) {
  if (typeof caches === "undefined" || !caches.default) return;
  try {
    await caches.default.put(
      dedupeUrl(key),
      new Response(String(value), {
        headers: { "cache-control": `max-age=${ttl}`, "content-type": "text/plain" },
      }),
    );
  } catch {
    // 缓存写失败不影响主流程，退回「实例内去重」——最坏情况还是可能重复，但不会发不出去
  }
}

async function dedupeDel(key) {
  if (typeof caches === "undefined" || !caches.default) return;
  try {
    await caches.default.delete(dedupeUrl(key));
  } catch {
    // 删不掉就让它在 TTL 后自然过期
  }
}
let feishuTokenCache = { token: "", exp: 0 };

async function sha256Hex(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** 飞书事件加密：AES-256-CBC，key=SHA256(encrypt_key)，iv=密文前 16 字节 */
async function decryptFeishu(encrypted, encryptKey) {
  const keyRaw = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(encryptKey));
  const bytes = Uint8Array.from(atob(encrypted.replace(/\s/g, "")), (c) => c.charCodeAt(0));
  const key = await crypto.subtle.importKey("raw", keyRaw, { name: "AES-CBC" }, false, ["decrypt"]);
  const plain = await crypto.subtle.decrypt(
    { name: "AES-CBC", iv: bytes.slice(0, 16) },
    key,
    bytes.slice(16),
  );
  return new TextDecoder().decode(plain);
}

async function feishuToken(env) {
  if (feishuTokenCache.token && Date.now() < feishuTokenCache.exp) return feishuTokenCache.token;
  const appId = (env.FEISHU_APP_ID || "").trim();
  const secret = (env.FEISHU_APP_SECRET || "").trim();
  if (!appId || !secret) throw new Error("未配置 FEISHU_APP_ID / FEISHU_APP_SECRET");

  const res = await fetch(`${FEISHU_API}/auth/v3/tenant_access_token/internal`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ app_id: appId, app_secret: secret }),
  });
  const data = await res.json();
  if (data.code !== 0) throw new Error(`tenant_access_token 失败 ${data.code} ${data.msg}`);

  // 提前 2 分钟过期，避免边界上拿到刚失效的 token
  const ttl = Math.max(60, (data.expire || 7200) - 120);
  feishuTokenCache = { token: data.tenant_access_token, exp: Date.now() + ttl * 1000 };
  return feishuTokenCache.token;
}

// 最近一次失败（回执 / 派发 / 处理异常）。/feishu/poll 会把它打出来，
// 否则线上只看到「处理 0 条」，然后去翻 /health 和 Cloudflare 日志才知道哪里坏了。
let lastFailure = { at: 0, where: "", detail: "" };

function noteFailure(where, detail) {
  const text = String(detail && detail.message ? detail.message : detail || "").slice(0, 300);
  lastFailure = { at: Date.now(), where, detail: text };
  console.error(`[FEISHU] ${where}失败：${text}`);
}

function failureLine() {
  if (!lastFailure.at) return "";
  const age = Math.round((Date.now() - lastFailure.at) / 1000);
  return `⚠️ 最近一次失败：${lastFailure.where} — ${lastFailure.detail}（${age} 秒前）`;
}

/** 230035 是最常见的坑，直接把解法写在报告里 */
const HINT_230035 =
  "（群发言权限被限制：群设置 → 群管理 → 谁可以发消息 → 改成「所有群成员」，或把机器人设为群管理员）";

/** 发一条文本消息，返回结果而不抛错 —— 回执失败不该阻断真正的派发 */
async function feishuSend(env, chatId, text) {
  try {
    const token = await feishuToken(env);
    const res = await fetch(`${FEISHU_API}/im/v1/messages?receive_id_type=chat_id`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
      body: JSON.stringify({
        receive_id: chatId,
        msg_type: "text",
        content: JSON.stringify({ text }),
      }),
    });
    const data = await res.json();
    if (data.code !== 0) {
      const detail = `${data.code} ${data.msg}${data.code === 230035 ? HINT_230035 : ""}`;
      noteFailure("回执发送", detail);
      return { ok: false, detail };
    }
    return { ok: true, detail: "" };
  } catch (err) {
    noteFailure("回执发送", err);
    return { ok: false, detail: String(err) };
  }
}

/** 带 inputs 的 dispatch（dispatchOne 故意不传 inputs，这里需要传代码与 chat_id） */
async function dispatchQuery(env, codes, chatId) {
  const owner = (env.OWNER || "Wind866").trim();
  const branch = (env.BRANCH || "main").trim();
  // QUERY_TARGET 是「仓库/工作流文件」，必须拆开拼：少写 /actions/workflows/ 会 404
  const [repo, workflow] = QUERY_TARGET.split("/");
  try {
    const headers = await authHeaders(env);
    const res = await fetch(
      `${API}/repos/${owner}/${repo}/actions/workflows/${workflow}/dispatches`,
      {
        method: "POST",
        headers: { ...headers, "content-type": "application/json" },
        // 带上 chat_id：workflow 跑完把结论发回提问的那个会话（群里问就回群里），
        // 而不是只发到 FEISHU_WEBHOOK 绑定的那个群
        body: JSON.stringify({
          ref: branch,
          inputs: { stocks: codes.join(","), period: "90d", chat_id: chatId || "" },
        }),
      },
    );
    if (res.status === 204) return { ok: true, detail: "" };
    const body = (await res.text()).slice(0, 160).replace(/\s+/g, " ");
    const detail =
      res.status === 401 || res.status === 403
        ? `HTTP ${res.status}（GH_TOKEN 过期或无 workflow 权限，去 /health?check=1 校验）${body}`
        : res.status === 404
          ? `HTTP 404（仓库或工作流文件名不对：${owner}/${repo}/actions/workflows/${workflow}）${body}`
          : `HTTP ${res.status} ${body}`;
    noteFailure("派发 Actions", detail);
    return { ok: false, detail };
  } catch (err) {
    noteFailure("派发 Actions", err);
    return { ok: false, detail: String(err) };
  }
}

/** 「600519, 000001 帮我看看」→ ['600519','000001']；纯闲聊返回空数组 */
function extractCodes(text) {
  // @机器人 在正文里是 @_user_1 这种占位符，先剔掉，避免误抓数字
  const cleaned = (text || "").replace(/@_user_\d+/g, " ");
  const found = cleaned.match(/(?<!\d)\d{6}(?!\d)/g) || [];
  return [...new Set(found)].slice(0, MAX_QUERY_CODES);
}

async function handleFeishuMessage(env, event) {
  const msg = (event && event.message) || {};
  const sender = (event && event.sender) || {};
  // 别回自己：机器人发的消息也会进事件流
  if ((sender.sender_type || "user") === "app") return;

  const chatId = msg.chat_id || "";
  if (!chatId) return;

  if (msg.message_type && msg.message_type !== "text") {
    await feishuSend(env, chatId, "只认文本股票代码，例如：600519 或 600519,000001（最多 6 只）");
    return;
  }

  let text = "";
  try {
    text = JSON.parse(msg.content || "{}").text || "";
  } catch {
    text = msg.content || "";
  }

  const codes = extractCodes(text);
  // 抓不到 6 位代码就当普通聊天，不回应 —— 否则机器人会在群里刷屏
  if (!codes.length) return;
  await handleQuery(env, codes, chatId, msg.message_id || "");
}

// 同一张单子 5 分钟内只通知一次（回执 / ❌ 都算），避免派发连续失败时每分钟刷屏；
// 失败本身仍会写进 /feishu/poll 报告，不会丢信息。
const notifyAt = new Map();
const NOTIFY_GAP_MS = 5 * 60 * 1000;

/**
 * 冷却 + 回执 + 触发。事件回调与轮询两条路径共用，保证行为一致。
 *
 * `msgKey` 是飞书的 message_id：同一条消息即使被两个 Worker 实例各扫到一次，
 * 也只会回执一次、派发一次（否则群里两条「已收到」，Actions 跑两遍推两张图）。
 *
 * 返回 { ok, skipped, receipt, dispatch }，skipped 取值：
 *   ""          正常处理
 *   "duplicate" 这条消息已被别的实例处理完，本次什么都不做
 *   "cooldown"  同代码 90 秒内刚派发过，只回一句提示
 */
async function handleQuery(env, codes, chatId, msgKey = "") {
  const key = codes.join(",");
  const now = Date.now();

  // ① 消息级去重：先查再占位，两个实例同时扫到也只有先到的那个往下走
  if (msgKey) {
    if (await dedupeGet(`msg:${msgKey}`)) {
      console.log(`[FEISHU] 消息 ${msgKey} 已处理过，跳过`);
      return {
        ok: true,
        skipped: "duplicate",
        receipt: { ok: true, detail: "" },
        dispatch: { ok: true, detail: "" },
      };
    }
    await dedupeSet(`msg:${msgKey}`, String(now));
  }

  // ② 代码级冷却：实例内的 Map 冷启动会丢，所以再查一层 Cache（跨实例生效）
  const last = Math.max(
    seenEvents.get(`q:${chatId}:${key}`) || 0,
    Number(await dedupeGet(`q:${chatId}:${key}`)) || 0,
  );
  if (now - last < CODE_COOLDOWN_MS) {
    const receipt = await feishuSend(env, chatId, `⏳ ${key} 刚查过，结果约 1 分钟到，请稍等`);
    return { ok: true, skipped: "cooldown", receipt, dispatch: { ok: true, detail: "" } };
  }

  const nkey = `${chatId}|${key}`;
  // 回执也要跨实例去重：不然派发失败重试时，新实例会再发一遍「已收到」
  const notifiedAt = Math.max(
    notifyAt.get(nkey) || 0,
    Number(await dedupeGet(`rcpt:${chatId}:${key}`)) || 0,
  );
  const fresh = Date.now() - notifiedAt > NOTIFY_GAP_MS;
  let receipt = { ok: true, detail: "", skipped: true };

  if (fresh) {
    console.log(`[FEISHU] 收到查询 ${key} chat=${chatId}`);
    // 回执发失败也要继续派发：群里没发出去不代表任务不该跑
    receipt = await feishuSend(env, chatId, `🔍 已收到 ${key}，正在跑真实数据（估值/资金流/财务），约 1~2 分钟把完整分析发到群里。`);
    notifyAt.set(nkey, Date.now());
    await dedupeSet(`rcpt:${chatId}:${key}`, String(Date.now()), Math.round(NOTIFY_GAP_MS / 1000));
    if (notifyAt.size > 200) notifyAt.clear();
  }

  const dispatched = await dispatchQuery(env, codes, chatId);
  if (dispatched.ok) {
    // 冷却只在真正派发成功后才记，否则失败一次就会把后续重试全挡掉
    seenEvents.set(`q:${chatId}:${key}`, Date.now());
    await dedupeSet(`q:${chatId}:${key}`, String(Date.now()));
    if (seenEvents.size > 200) seenEvents.clear();
  } else {
    // 派发失败：撤掉消息占位，让下一轮（或另一个实例）还能重试
    if (msgKey) await dedupeDel(`msg:${msgKey}`);
    if (fresh) {
      await feishuSend(env, chatId, `❌ 触发分析任务失败：${dispatched.detail}`);
    }
  }
  return { ok: dispatched.ok, skipped: "", receipt, dispatch: dispatched };
}

// ===== 轮询模式：Worker 主动去群里捞「@机器人 + 6 位代码」 =====
//
// 为什么不用事件订阅
// ----------------
// 飞书回调要求一个公网 HTTPS 地址，而 *.workers.dev 在国内实测 200+ 节点全部超时
// （只有港澳台通），飞书的回调服务器在国内机房，请求根本发不出来 —— 表现为
// 「请求 3 秒超时」或「url invalid」。换成自建域名能解决，但要买域名、改 NS、等生效。
//
// 反过来做：Worker 每分钟本来就会被 cron 唤醒，顺手调飞书「获取会话历史消息」接口，
// 把最近几分钟里 @了机器人 的消息捞出来处理。**不需要任何公网回调地址**，
// 因此也不需要域名、不需要事件订阅、不受 3 秒超时限制。
//
// 代价：响应延迟最多一个轮询间隔（这里取 2 分钟），不是秒回。
//
// 需要的权限（飞书开放平台 → 权限管理，开完必须发布版本）
//   im:message 或 im:message:readonly 或 im:message.history:readonly
//                                 读群历史消息（im/v1/messages 三者任一即可）
//   im:message:send_as_bot        以机器人身份发消息
//   im:chat:readonly              获取机器人所在群列表（自动找 chat_id 用，可选：
//                                 直接在变量里配 FEISHU_CHAT_ID 就不需要这个权限）

const POLL_EVERY_MIN = 1;                   // 每 1 分钟轮询一次
const POLL_WINDOW_MS = 5 * 60 * 1000;       // 热实例：每次看最近 5 分钟的消息
const POLL_FIRST_LOOKBACK_MS = 90 * 1000;   // 冷实例首次轮询只回看 90 秒，避免把历史消息全部重跑
// 必须保证 POLL_FIRST_LOOKBACK_MS > POLL_EVERY_MIN*60s，否则冷实例之间有消息会漏
// （cron 每分钟唤醒，但实例不保证复用，所以按「最坏情况每次都是冷实例」来设）

const pollSeen = new Map();                 // message_id 去重（实例级）
let pollBootAt = 0;
let botInfoCache = { id: "", name: "", exp: 0 };

/** 机器人自己的 open_id，用来判断消息里 @ 的是不是它 */
async function feishuBotInfo(env) {
  if (botInfoCache.id && Date.now() < botInfoCache.exp) return botInfoCache;
  const token = await feishuToken(env);
  const res = await fetch(`${FEISHU_API}/bot/v3/info`, {
    headers: { authorization: `Bearer ${token}` },
  });
  const data = await res.json();
  if (data.code !== 0) throw new Error(`bot/v3/info 失败 ${data.code} ${data.msg}`);
  const bot = data.bot || {};
  botInfoCache = { id: bot.open_id || "", name: bot.app_name || "", exp: Date.now() + 3600 * 1000 };
  return botInfoCache;
}

/** 机器人所在的群列表。配了 FEISHU_CHAT_ID 就不用调这个（免 im:chat:readonly 权限） */
async function feishuChats(env) {
  const token = await feishuToken(env);
  const res = await fetch(`${FEISHU_API}/im/v1/chats?page_size=50`, {
    headers: { authorization: `Bearer ${token}` },
  });
  const data = await res.json();
  if (data.code !== 0) throw new Error(`im/v1/chats 失败 ${data.code} ${data.msg}`);
  return ((data.data || {}).items || []).map((c) => c.chat_id).filter(Boolean);
}

/** 某个群最近的消息（按时间倒序返回） */
async function feishuRecentMessages(env, chatId) {
  const token = await feishuToken(env);
  const url = new URL(`${FEISHU_API}/im/v1/messages`);
  // 注意：容器类型是 chat（不是 chat_id）；receive_id_type=chat_id 是「发送消息」接口的写法
  url.searchParams.set("container_id_type", "chat");
  url.searchParams.set("container_id", chatId);
  url.searchParams.set("sort_type", "ByCreateTimeDesc");
  url.searchParams.set("page_size", "20");
  const res = await fetch(url.toString(), { headers: { authorization: `Bearer ${token}` } });
  const data = await res.json();
  if (data.code !== 0) throw new Error(`im/v1/messages 失败 ${data.code} ${data.msg}`);
  return (data.data || {}).items || [];
}

/**
 * 消息时间戳统一成毫秒。
 * 飞书不同接口/版本给过秒也给过毫秒（字符串形式都一样），这里按量级判断，
 * 免得因为单位不一致把消息全过滤掉（表现为「最近 20 条，窗口内 0 条」）。
 */
function msgTimeMs(it) {
  const raw = it && it.create_time != null ? it.create_time : (it && it.body ? it.body.create_time : undefined);
  const t = Number(raw);
  if (!Number.isFinite(t) || t <= 0) return 0;
  return t < 1e12 ? t * 1000 : t;
}

/** 从消息体里把可读文本捞出来：text 直接取，post（富文本）递归拼 */
function messageText(it) {
  const rawContent = (it.body || {}).content || "";
  let obj;
  try {
    obj = JSON.parse(rawContent);
  } catch {
    return rawContent;
  }
  if (typeof obj.text === "string") return obj.text;
  const parts = [];
  const walk = (n) => {
    if (!n) return;
    if (Array.isArray(n)) {
      n.forEach(walk);
      return;
    }
    if (typeof n === "object") {
      if (typeof n.text === "string") parts.push(n.text);
      if (n.tag === "at" && n.user_name) parts.push("@" + n.user_name);
      if (n.content) walk(n.content);
    }
  };
  walk(obj.title);
  walk(obj.content);
  return parts.join(" ");
}

/** 消息里的 @ 列表（open_id / user_id 都收，不同接口给的不一样） */
function mentionsOf(it) {
  return (it.mentions || [])
    .map((m) => {
      const id = m.id || {};
      return { id: id.open_id || id.user_id || "", name: m.name || "" };
    })
    .filter((m) => m.id || m.name);
}

/** 这条消息是不是 @ 了本机器人 */
function isMentionedBot(it, bot) {
  const mentions = mentionsOf(it);
  if (bot.id && mentions.some((m) => m.id === bot.id)) return true;
  // 兼容只给名字、或 open_id 与 bot/v3/info 不一致的情况
  if (bot.name && mentions.some((m) => m.name === bot.name)) return true;
  if (bot.name && messageText(it).includes("@" + bot.name)) return true;
  return false;
}

/**
 * 判定一条消息要不要处理，并给出「为什么跳过」——
 * /feishu/poll 会把它原样显示出来，省得反复猜（以前只有「命中」和沉默两种结果）。
 */
function evaluateMessage(it, bot) {
  const type = it.msg_type || "?";
  const senderType = (it.sender || {}).sender_type || "user";
  const text = messageText(it);
  const codes = extractCodes(text);
  const mentions = mentionsOf(it);

  let skip = "";
  if (senderType === "app") skip = "机器人自己发的";
  else if (type !== "text" && type !== "post") skip = `类型 ${type}`;
  else if (!isMentionedBot(it, bot)) skip = "没@机器人";
  else if (!codes.length) skip = "文里没有 6 位代码";

  return { type, senderType, text, codes, mentions, skip };
}

/**
 * 扫一遍所有群，处理 @机器人 + 6 位代码 的消息。
 * 返回一份人类可读的报告（/feishu/poll 会直接把它显示在浏览器里，便于自检）。
 */
async function pollFeishu(env) {
  const report = [];
  const appId = (env.FEISHU_APP_ID || "").trim();
  const secret = (env.FEISHU_APP_SECRET || "").trim();
  if (!appId || !secret) return "未配置 FEISHU_APP_ID / FEISHU_APP_SECRET，轮询跳过";

  if (!pollBootAt) pollBootAt = Date.now();
  // 只处理最近一段时间的消息。接口返回的 create_time 是**毫秒**时间戳字符串，
  // 这里本地过滤，不用 start_time 参数（那个参数的单位和「是否必须成对」都容易踩坑）。
  const from = Math.max(Date.now() - POLL_WINDOW_MS, pollBootAt - POLL_FIRST_LOOKBACK_MS);

  const bot = await feishuBotInfo(env);
  report.push(`机器人：${bot.name || "(未命名)"} open_id=${bot.id || "?"}`);
  report.push(`时间窗口：最近 ${Math.round((Date.now() - from) / 1000)} 秒（轮询间隔 ${POLL_EVERY_MIN} 分钟）`);

  const fixed = (env.FEISHU_CHAT_ID || "").trim();
  const chatIds = fixed ? [fixed] : await feishuChats(env);
  report.push(`扫描群数：${chatIds.length}${fixed ? "（来自 FEISHU_CHAT_ID）" : ""}`);
  if (!chatIds.length) report.push("机器人不在任何群里？把它拉进目标群再试");

  let handled = 0;
  for (const chatId of chatIds) {
    let items = [];
    try {
      items = await feishuRecentMessages(env, chatId);
    } catch (err) {
      report.push(`  ${chatId} 拉取失败：${err}`);
      continue;
    }
    const fetched = items.length;
    const newest = items.reduce((mx, it) => Math.max(mx, msgTimeMs(it)), 0);
    items = items.filter((it) => {
      const t = msgTimeMs(it);
      return t > 0 ? t >= from : true;
    });
    const newestText = newest
      ? `最新一条 ${Math.max(0, Math.round((Date.now() - newest) / 1000))} 秒前`
      : "读不到时间戳";
    report.push(`  ${chatId} 最近 ${fetched} 条（${newestText}），落在窗口内 ${items.length} 条`);

    let shown = 0;
    // 接口是时间倒序，翻过来正序处理，保证「先问先答」
    for (const it of items.slice().reverse()) {
      const id = it.message_id || "";
      const v = evaluateMessage(it, bot);
      const seen = Boolean(id) && pollSeen.has(id);

      // 把判定结果打出来（即使已经处理过、即使要跳过），否则线上只能看到「处理 0 条」干瞪眼
      if (shown < 6) {
        shown += 1;
        const age = Math.max(0, Math.round((Date.now() - msgTimeMs(it)) / 1000));
        const mt = v.mentions.length ? v.mentions.map((m) => m.id || m.name).join("|") : "无";
        let verdict;
        if (v.skip) verdict = `跳过（${v.skip}）`;
        else if (seen) verdict = `已在上一轮处理过 ${v.codes.join(",")}`;
        else verdict = `触发 ${v.codes.join(",")}`;
        report.push(`    · ${id.slice(-8) || "无id"} ${age}s前 ${v.type}/${v.senderType} 提及=${mt} 代码=${v.codes.join(",") || "无"} → ${verdict}`);
      }
      if (v.skip || seen || !id) continue;

      try {
        const r = await handleQuery(env, v.codes, chatId, id);
        if (r.skipped === "duplicate") {
          // 另一个实例已经处理过这条（冷启动时本实例看不见），这里一声都不出
          pollSeen.set(id, Date.now());
          report.push(`      ↳ 已被别的实例处理过，本次跳过（跨实例去重生效）`);
          continue;
        }
        if (r.ok) {
          pollSeen.set(id, Date.now());
          handled += 1;
          const note = r.receipt && r.receipt.ok ? "" : `（回执没发出去：${(r.receipt || {}).detail || "未知"}）`;
          report.push(`      ↳ ${r.skipped === "cooldown" ? "刚查过，跳过派发" : "已派发"} ${v.codes.join(",")}${note}`);
        } else {
          // 不记入 pollSeen，下一轮自动重试
          report.push(`      ↳ 派发失败：${r.dispatch.detail}（下一轮重试）`);
        }
      } catch (err) {
        noteFailure("处理消息", err);
        report.push(`      ↳ 处理异常：${err}（下一轮重试）`);
      }
    }
  }

  if (pollSeen.size > 500) pollSeen.clear();
  const fail = failureLine();
  if (fail) report.push(fail);
  report.push(`本次处理 ${handled} 条查询`);
  return report.join("\n");
}

async function handleFeishu(req, env, ctx) {
  const raw = await req.text();
  const encryptKey = (env.FEISHU_ENCRYPT_KEY || "").trim();

  // 签名只在配了 Encrypt Key 时下发，没有就跳过校验（明文模式飞书不签名）
  const timestamp = req.headers.get("X-Lark-Request-Timestamp") || "";
  const nonce = req.headers.get("X-Lark-Request-Nonce") || "";
  const signature = req.headers.get("X-Lark-Signature") || "";
  if (encryptKey && signature) {
    const expect = await sha256Hex(timestamp + nonce + encryptKey + raw);
    if (expect !== signature) {
      console.log("[FEISHU] 签名校验失败，已拒绝");
      return json({ code: 1, msg: "invalid signature" }, 401);
    }
  }

  let payload;
  try {
    payload = JSON.parse(raw);
  } catch {
    return json({ code: 1, msg: "bad json" }, 400);
  }

  if (payload.encrypt) {
    if (!encryptKey) {
      return json({ code: 1, msg: "event encrypted but FEISHU_ENCRYPT_KEY missing" }, 400);
    }
    try {
      payload = JSON.parse(await decryptFeishu(payload.encrypt, encryptKey));
    } catch (err) {
      console.error(`[FEISHU] 解密失败 ${err}`);
      return json({ code: 1, msg: "decrypt failed" }, 400);
    }
  }

  // 在开放平台保存请求地址时飞书会先打一发，必须原样回 challenge
  if (payload.type === "url_verification") {
    return json({ challenge: payload.challenge });
  }

  const header = payload.header || {};
  const eventId = header.event_id || "";
  if (eventId && seenEvents.has(`evt:${eventId}`)) return json({ code: 0 });
  if (eventId) seenEvents.set(`evt:${eventId}`, Date.now());

  if (header.event_type === "im.message.receive_v1") {
    // 飞书 3 秒内收不到 200 会重投，所以先回 200，慢活在 waitUntil 里跑
    ctx.waitUntil(handleFeishuMessage(env, payload.event));
    return json({ code: 0 });
  }

  console.log(`[FEISHU] 忽略事件 ${header.event_type || payload.type || "unknown"}`);
  return json({ code: 0 });
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

    // 每 2 分钟去飞书群里捞一次 @机器人 的消息（替代不可用的公网回调）
    if (Number(bj.hhmm.slice(3)) % POLL_EVERY_MIN === 0) {
      ctx.waitUntil(pollFeishu(env).catch((err) => console.error(`[POLL] ${err}`)));
    }
  },

  async fetch(req, env, ctx) {
    const url = new URL(req.url);

    // 飞书自建应用的事件订阅入口：群里发 6 位代码 → dispatch stock-query.yml
    if (url.pathname === "/feishu") {
      if (req.method !== "POST") {
        return new Response("飞书事件回调端点（接收 POST）。把本地址填进飞书开放平台的事件订阅请求地址。\n", {
          headers: { "content-type": "text/plain; charset=utf-8" },
        });
      }
      return handleFeishu(req, env, ctx);
    }

    // 手动跑一次轮询，报告直接显示在浏览器里 —— 配完权限后用这个自检最快
    if (url.pathname === "/feishu/poll") {
      try {
        const text = await pollFeishu(env);
        return new Response(`${text}\n`, { headers: { "content-type": "text/plain; charset=utf-8" } });
      } catch (err) {
        return new Response(`轮询失败：${err}\n`, { status: 500, headers: { "content-type": "text/plain; charset=utf-8" } });
      }
    }

    // 通道自检：逐项验飞书 token / 机器人 / 群 / GH token，加 ?send=1 再真发一条到群里。
    // 专门用来把「群没给机器人发言权限（230035）」这类问题当场暴露，不用去翻日志。
    if (url.pathname === "/feishu/selftest") {
      const lines = [];
      const chatId = (url.searchParams.get("chat_id") || env.FEISHU_CHAT_ID || "").trim();
      try {
        await feishuToken(env);
        lines.push("飞书 tenant_access_token：OK");
      } catch (err) {
        lines.push(`飞书 tenant_access_token：失败 ${err}`);
        return new Response(lines.join("\n") + "\n", { headers: { "content-type": "text/plain; charset=utf-8" } });
      }
      try {
        const bot = await feishuBotInfo(env);
        lines.push(`机器人：${bot.name || "(未命名)"} ${bot.id}`);
      } catch (err) {
        lines.push(`机器人信息：失败 ${err}`);
      }
      const target = chatId || (await feishuChats(env).catch(() => []))[0] || "";
      lines.push(`目标群：${target || "（没找到，配置 FEISHU_CHAT_ID 或把机器人拉进群）"}`);
      try {
        const headers = await authHeaders(env);
        const res = await fetch(`${API}/user`, { headers });
        // classic token 会带 x-oauth-scopes；没有 workflow 就 dispatch 不了 workflow（会 403）
        const scopes = res.headers.get("x-oauth-scopes") || "";
        const perms = res.headers.get("x-accepted-github-permissions") || "";
        let line = `GitHub token：${res.ok ? "OK" : `失败 HTTP ${res.status}`}`;
        if (scopes) line += `（scopes: ${scopes}）`;
        if (perms) line += `（可用权限: ${perms}）`;
        lines.push(line);
        if (res.ok && scopes && !/\bworkflow\b/.test(scopes) && !/\brepo\b/.test(scopes)) {
          lines.push("⚠️ 这个 classic token 没有 workflow 权限，派发 workflow 会 403 —— 重新生成时勾上 workflow");
        }
      } catch (err) {
        lines.push(`GitHub token：失败 ${err}`);
      }
      // workflow 元信息可达性：仓库名 / 文件名任一写错都会 404，
      // 这正是「派发失败 HTTP 404」的根因，所以自检必须单独看一眼这条 URL
      try {
        const headers = await authHeaders(env);
        const [repo, workflow] = QUERY_TARGET.split("/");
        const wfUrl = `${API}/repos/${(env.OWNER || "Wind866").trim()}/${repo}/actions/workflows/${workflow}`;
        const res = await fetch(wfUrl, { headers });
        lines.push(
          res.ok
            ? `个股查询 workflow：OK（${repo}/actions/workflows/${workflow}）`
            : `个股查询 workflow：失败 HTTP ${res.status}（${wfUrl}）`,
        );
      } catch (err) {
        lines.push(`个股查询 workflow：异常 ${err}`);
      }
      if (url.searchParams.get("dispatch") === "1") {
        // 真调一次 dispatch（目标是对账工作流，只读、无副作用），用来验证 token 到底能不能派发
        try {
          const headers = await authHeaders(env);
          const res = await fetch(`${API}/repos/${(env.OWNER || "Wind866").trim()}/BIGApush/actions/workflows/schedule-audit.yml/dispatches`, {
            method: "POST",
            headers: { ...headers, "content-type": "application/json" },
            body: JSON.stringify({ ref: (env.BRANCH || "main").trim() }),
          });
          lines.push(
            res.status === 204
              ? "派发能力测试：OK（顺带触发了一次档期对账，只读无副作用）"
              : `派发能力测试：失败 HTTP ${res.status} ${(await res.text()).slice(0, 140)}`
          );
        } catch (err) {
          lines.push(`派发能力测试：异常 ${err}`);
        }
      }
      if (url.searchParams.get("send") === "1") {
        if (!target) {
          lines.push("发送测试：跳过（没有目标群）");
        } else {
          const r = await feishuSend(env, target, "【自检】png推送 发送通道测试，可忽略");
          lines.push(r.ok ? "发送测试：成功（群里应该多了一条自检消息）" : `发送测试：失败 ${r.detail}`);
        }
      } else {
        lines.push("发送测试：未执行（加 ?send=1 会真的往群里发一条）");
      }
      return new Response(lines.join("\n") + "\n", { headers: { "content-type": "text/plain; charset=utf-8" } });
    }

    if (url.pathname === "/health") {
      let tokenValid = null;
      let tokenScopes = null;
      if (url.searchParams.get("check") === "1") {
        try {
          const res = await fetch(`${API}/user`, { headers: await authHeaders(env) });
          tokenValid = res.ok;
          tokenScopes = res.headers.get("x-oauth-scopes") || (res.ok ? "(fine-grained, 无 scopes 头)" : "");
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
        tokenScopes,
        alertConfigured: Boolean((env.ALERT_WEBHOOK || "").trim()),
        feishuApp: Boolean((env.FEISHU_APP_ID || "").trim()),
        feishuPollEveryMin: POLL_EVERY_MIN,
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
      "GitHub Actions 统一准点调度器\n\n/health            查看档期表与配置状态\n/health?check=1     校验 GH_TOKEN\n/test?at=22:13     手动触发某个档期（自测用）\n/feishu/poll       立刻轮询一次飞书群消息（群里 @机器人 发股票代码用这个，不需要公网回调）\n/feishu            POST，飞书事件回调用（仅在你有公网域名时才需要）\n",
      { headers: { "content-type": "text/plain; charset=utf-8" } },
    );
  },
};
