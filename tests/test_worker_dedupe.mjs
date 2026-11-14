// 回归测试：Worker 轮询「同一件事只做一次」
//
// 背景（线上真实故障）：cron 每分钟唤醒一次，但 Cloudflare 不保证复用实例。
// 新实例的 pollSeen / seenEvents / notifyAt 全是空的，而冷启动回看 90 秒 > 轮询
// 间隔 60 秒 —— 同一条「@机器人 002131」被上一分钟的实例和这一分钟的实例各处理
// 一遍，群里出现两条「已收到」，Actions 也跑两遍（推两张图）。
//
// 这里用「同一个 Cache + 两份独立的模块实例」精确复现两次冷启动：
//   node tests/test_worker_dedupe.mjs
import assert from "node:assert/strict";

const WORKER = new URL("../cloudflare/worker.js", import.meta.url).href;
const CHAT = "oc_test";
const code = "002131";

// ---------------------------------------------------------------- 假环境
let store = new Map();
let counters = { receipt: 0, failed: 0, cooldown: 0, dispatch: 0 };
let items = []; // 群里「最近的消息」，按时间倒序（与飞书接口一致）
let dispatchFails = 0;

globalThis.caches = {
  default: {
    async match(req) {
      const key = typeof req === "string" ? req : req.url;
      const hit = store.get(key);
      return hit ? new Response(hit.value, { headers: hit.headers }) : undefined;
    },
    async put(req, res) {
      const key = typeof req === "string" ? req : req.url;
      store.set(key, { value: await res.text(), headers: res.headers });
    },
    async delete(req) {
      const key = typeof req === "string" ? req : req.url;
      store.delete(key);
    },
  },
};

globalThis.fetch = async (url, opts = {}) => {
  const u = String(url);
  const method = (opts.method || "GET").toUpperCase();
  const ok = (obj) =>
    new Response(JSON.stringify(obj), { headers: { "content-type": "application/json" } });

  if (u.includes("/auth/v3/tenant_access_token/internal")) {
    return ok({ code: 0, tenant_access_token: "t-fake", expire: 7200 });
  }
  if (u.includes("/bot/v3/info")) {
    return ok({ code: 0, bot: { open_id: "ou_bot", app_name: "png推送" } });
  }
  if (u.includes("/im/v1/messages") && method === "POST") {
    // 按文案区分三种群消息，否则「❌ 失败」「⏳ 刚查过」都会被算成回执
    let text = "";
    try {
      text = JSON.parse(JSON.parse(opts.body).content).text || "";
    } catch {
      text = "";
    }
    if (text.startsWith("🔍")) counters.receipt += 1;
    else if (text.startsWith("❌")) counters.failed += 1;
    else if (text.startsWith("⏳")) counters.cooldown += 1;
    return ok({ code: 0, data: { message_id: "om_receipt" } });
  }
  if (u.includes("/im/v1/messages")) {
    return ok({ code: 0, data: { items } });
  }
  if (u.includes("/actions/workflows/") && u.endsWith("/dispatches")) {
    counters.dispatch += 1;
    if (dispatchFails > 0) {
      dispatchFails -= 1;
      return new Response("boom", { status: 500 });
    }
    return new Response(null, { status: 204 });
  }
  throw new Error(`未处理的请求 ${method} ${u}`);
};

const env = {
  FEISHU_APP_ID: "cli_x",
  FEISHU_APP_SECRET: "sec",
  FEISHU_CHAT_ID: CHAT,
  GH_TOKEN: "ghp_x",
  OWNER: "Wind866",
  BRANCH: "main",
};

let instance = 0;
/** 每次调用都拿到一份全新模块状态 —— 等价于一次冷启动 */
async function coldBoot() {
  instance += 1;
  return (await import(`${WORKER}?cold=${instance}`)).default;
}

async function pollOf(mod) {
  const res = await mod.fetch(new Request("https://w.dev/feishu/poll"), env, { waitUntil() {} });
  return res.text();
}

function textMessage(id, secondsAgo) {
  return {
    message_id: id,
    create_time: String(Date.now() - secondsAgo * 1000),
    msg_type: "text",
    sender: { sender_type: "user", id: "ou_user" },
    body: { content: JSON.stringify({ text: `@_user_1 ${code} 看看` }) },
    mentions: [{ key: "@_user_1", id: { open_id: "ou_bot" }, name: "png推送" }],
  };
}

function reset() {
  store = new Map();
  counters = { receipt: 0, failed: 0, cooldown: 0, dispatch: 0 };
  dispatchFails = 0;
  items = [];
}

// ---------------------------------------------------------------- 用例
const cases = [];
const test = (name, fn) => cases.push([name, fn]);

test("两次冷启动扫到同一条消息：只回执一次、只派发一次", async () => {
  reset();
  items = [textMessage("om_dup_1", 20)];
  await pollOf(await coldBoot());
  const report = await pollOf(await coldBoot());
  assert.equal(counters.receipt, 1, "回执应只有 1 条");
  assert.equal(counters.dispatch, 1, "派发应只有 1 次");
  assert.match(report, /已被别的实例处理过/, "报告里应写明被去重拦住");
});

test("派发失败：撤掉消息占位，下一个实例能重试成功且不再发「已收到」", async () => {
  reset();
  items = [textMessage("om_dup_2", 20)];
  dispatchFails = 1;
  await pollOf(await coldBoot()); // 第一次：派发 500
  assert.equal(counters.dispatch, 1);
  await pollOf(await coldBoot()); // 第二个实例：应重试
  assert.equal(counters.dispatch, 2, "失败后必须允许重试");
  assert.equal(counters.receipt, 1, "重试不该再发一条「已收到」");
  assert.equal(counters.failed, 1, "第一次失败应告诉群里");
});

test("同一代码换条消息再问一次：只回「刚查过」，不重复派发", async () => {
  reset();
  items = [textMessage("om_dup_3", 20)];
  await pollOf(await coldBoot());
  assert.equal(counters.dispatch, 1);

  // 30 秒后同一个人又 @ 了一次（新 message_id）
  items = [textMessage("om_dup_4", 2)];
  const report = await pollOf(await coldBoot());
  assert.equal(counters.dispatch, 1, "90 秒冷却内不该再派发");
  assert.equal(counters.cooldown, 1, "应回一条「刚查过」提示");
  assert.match(report, /刚查过，跳过派发/);
});

test("不是 @机器人 的消息：一次都不回", async () => {
  reset();
  const plain = textMessage("om_dup_5", 10);
  plain.mentions = [];
  plain.body = { content: JSON.stringify({ text: `${code} 看看` }) };
  items = [plain];
  await pollOf(await coldBoot());
  assert.equal(counters.receipt, 0);
  assert.equal(counters.dispatch, 0);
});

let failed = 0;
for (const [name, fn] of cases) {
  try {
    await fn();
    console.log(`PASS  ${name}`);
  } catch (err) {
    failed += 1;
    console.error(`FAIL  ${name}\n      ${err.message}`);
  }
}

console.log(failed ? `\n${failed}/${cases.length} 条用例失败` : `\n${cases.length}/${cases.length} 条用例通过`);
process.exit(failed ? 1 : 0);
