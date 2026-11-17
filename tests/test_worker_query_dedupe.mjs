// 回归测试：同一会话 + 同一组代码，5 分钟内只派发一次 stock-query。
// 覆盖 Cache API 跨 colo 失效、以及轮询 / 事件回调双路径各扫到一次的场景。
//   node tests/test_worker_query_dedupe.mjs
//   WORKER_PATH=/abs/path/worker.js node tests/test_worker_query_dedupe.mjs
import assert from "node:assert/strict";
import path from "node:path";
import { pathToFileURL } from "node:url";

const WORKER = process.env.WORKER_PATH
  ? pathToFileURL(path.resolve(process.env.WORKER_PATH)).href
  : new URL("../cloudflare/worker.js", import.meta.url).href;

const CHAT = "oc_query_dedupe";
let store = new Map();
let runs = [];
let counters = { receipt: 0, dispatch: 0, skippedRecent: 0 };
let items = [];

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

const ok = (obj) =>
  new Response(JSON.stringify(obj), { headers: { "content-type": "application/json" } });

globalThis.fetch = async (url, opts = {}) => {
  const u = String(url);
  let body = {};
  try { body = JSON.parse(opts.body || "{}"); } catch { /* text payload */ }

  if (u.includes("/auth/v3/tenant_access_token/internal")) {
    return ok({ code: 0, tenant_access_token: "t-fake", expire: 7200 });
  }
  if (u.includes("/bot/v3/info")) {
    return ok({ code: 0, bot: { open_id: "ou_bot", app_name: "png推送" } });
  }
  if (u.includes("/im/v1/messages") && (opts.method || "GET").toUpperCase() === "POST") {
    let text = "";
    try { text = JSON.parse(JSON.parse(opts.body).content).text || ""; } catch { /* ignore */ }
    if (text.startsWith("🔍")) counters.receipt += 1;
    return ok({ code: 0, data: { message_id: "om_receipt" } });
  }
  if (u.includes("/im/v1/messages")) return ok({ code: 0, data: { items } });
  if (u.includes("/actions/workflows/") && u.includes("/runs?")) {
    return ok({ workflow_runs: runs });
  }
  if (u.includes("/actions/workflows/") && u.endsWith("/dispatches")) {
    counters.dispatch += 1;
    runs.unshift({
      event: "workflow_dispatch",
      created_at: new Date().toISOString(),
      name: `个股查询 CODES=${body.inputs?.stocks || ""} CHAT=${body.inputs?.chat_id || ""}`,
    });
    return new Response(null, { status: 204 });
  }
  throw new Error(`未处理的请求 ${opts.method || "GET"} ${u}`);
};

const env = {
  FEISHU_APP_ID: "cli_x",
  FEISHU_APP_SECRET: "sec",
  FEISHU_CHAT_ID: CHAT,
  GH_TOKEN: "ghp_x",
  OWNER: "Wind866",
  BRANCH: "main",
  ADMIN_TOKEN: "test-admin-token",
};

let instance = 0;
async function coldBoot() {
  instance += 1;
  return (await import(`${WORKER}?qdedupe=${instance}`)).default;
}
async function poll(mod) {
  const res = await mod.fetch(
    new Request(`https://w.dev/feishu/poll?key=${env.ADMIN_TOKEN}`),
    env,
    { waitUntil() {} },
  );
  return res.text();
}
function msg(id, secondsAgo, codes) {
  return {
    message_id: id,
    create_time: String(Date.now() - secondsAgo * 1000),
    msg_type: "text",
    sender: { sender_type: "user" },
    body: { content: JSON.stringify({ text: `@_user_1 ${codes} 看看` }) },
    mentions: [{ id: { open_id: "ou_bot" }, name: "png推送" }],
  };
}
function reset() {
  store = new Map();
  runs = [];
  counters = { receipt: 0, dispatch: 0, skippedRecent: 0 };
  items = [];
}

const cases = [];
const test = (name, fn) => cases.push([name, fn]);

test("跨冷启动 + 新消息：Actions 上已有同单，跳过第二次派发", async () => {
  reset();
  items = [msg("om_q_1", 20, "600519")];
  await poll(await coldBoot());
  assert.equal(counters.dispatch, 1);

  // 模拟落到另一个 colo：Cache 全空，但 GitHub 上已有 dispatch 记录
  store = new Map();
  items = [msg("om_q_2", 2, "600519")];
  const report = await poll(await coldBoot());
  assert.equal(counters.dispatch, 1, "5 分钟内同一会话 + 同代码不该再派发");
  assert.match(report, /5 分钟内已有同单，跳过重复派发/);
});

test("不同代码不误判：新查询照常派发", async () => {
  reset();
  items = [msg("om_q_3", 20, "600519")];
  await poll(await coldBoot());
  store = new Map();
  items = [msg("om_q_4", 2, "000001")];
  await poll(await coldBoot());
  assert.equal(counters.dispatch, 2, "不同股票是两单，不应被幂等闸挡住");
});

test("运行页标题没有 CODES 标记时不误判：照常派发", async () => {
  reset();
  runs = [{ event: "workflow_dispatch", created_at: new Date().toISOString(), name: "个股查询" }];
  items = [msg("om_q_5", 20, "600519")];
  await poll(await coldBoot());
  assert.equal(counters.dispatch, 1, "老版本 run-name 不带标记时不能挡住查询");
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
console.log(`\n${cases.length - failed}/${cases.length} passed`);
process.exit(failed ? 1 : 0);
