// 补充用例：验证「D1 原子认领」与「GitHub 守卫」两层新逻辑
// 仓库自带的 test_worker_dedupe.mjs 只覆盖 Cache 那一层，这里补齐。
import assert from "node:assert/strict";

const WORKER = new URL("../cloudflare/worker.js", import.meta.url).href;
const CHAT = "oc_live_test";
const CODE = "301178";

let store = new Map();
let counters = { receipt: 0, failed: 0, cooldown: 0, dispatch: 0 };
let items = [];
let runs = [];            // 模拟 GitHub 上已有的运行记录
let dispatchStatus = [];  // 依次消费：204 / 500
let acceptedOn500 = false;

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

// 极简 D1 桩：只实现 INSERT OR IGNORE / UPDATE / DELETE，主键冲突返回 changes=0
function makeDB(sharedRows) {
  const rows = sharedRows || new Map();
  return {
    _rows: rows,
    prepare(sql) {
      let args = [];
      const stmt = {
        bind(...a) { args = a; return stmt; },
        async run() {
          if (/INSERT\s+OR\s+IGNORE/i.test(sql)) {
            const [msgId] = args;
            if (rows.has(msgId)) return { meta: { changes: 0 } };
            rows.set(msgId, { at: args[3] });
            return { meta: { changes: 1 } };
          }
          if (/^\s*DELETE/i.test(sql)) {
            const [msgId] = args;
            rows.delete(msgId);
            return { meta: { changes: 1 } };
          }
          return { meta: { changes: 1 } };
        },
        async first() { return null; },
      };
      return stmt;
    },
  };
}

globalThis.fetch = async (url, opts = {}) => {
  const u = String(url);
  const method = (opts.method || "GET").toUpperCase();
  const ok = (obj) =>
    new Response(JSON.stringify(obj), { headers: { "content-type": "application/json" } });

  if (u.includes("/auth/v3/tenant_access_token/internal")) {
    return ok({ code: 0, tenant_access_token: "t", expire: 7200 });
  }
  if (u.includes("/bot/v3/info")) {
    return ok({ code: 0, bot: { open_id: "ou_bot", app_name: "png推送" } });
  }
  if (u.includes("/im/v1/messages") && method === "POST") {
    let text = "";
    try { text = JSON.parse(JSON.parse(opts.body).content).text || ""; } catch { text = ""; }
    if (text.startsWith("🔍")) counters.receipt += 1;
    else if (text.startsWith("❌")) counters.failed += 1;
    else if (text.startsWith("⏳")) counters.cooldown += 1;
    return ok({ code: 0, data: { message_id: "om_r" } });
  }
  if (u.includes("/im/v1/messages")) {
    return ok({ code: 0, data: { items } });
  }
  // 运行记录：守卫读这里
  if (/\/actions\/workflows\/[^/]+\/runs/.test(u) && method === "GET") {
    return ok({ workflow_runs: runs });
  }
  if (u.endsWith("/dispatches")) {
    counters.dispatch += 1;
    const st = dispatchStatus.length ? dispatchStatus.shift() : 204;
    if (st === 204 || (st === 500 && acceptedOn500)) {
      // GitHub 受理后会立刻出现一条运行记录，name 就是 run-name
      runs.unshift({
        event: "workflow_dispatch",
        created_at: new Date().toISOString(),
        name: `个股查询 CODES=${CODE} CHAT=${CHAT}`,
        display_title: `个股查询 CODES=${CODE} CHAT=${CHAT}`,
      });
    }
    return st === 204 ? new Response(null, { status: 204 }) : new Response("boom", { status: st });
  }
  throw new Error(`未处理的请求 ${method} ${u}`);
};

const baseEnv = {
  FEISHU_APP_ID: "cli_x",
  FEISHU_APP_SECRET: "sec",
  FEISHU_CHAT_ID: CHAT,
  GH_TOKEN: "ghp_x",
  OWNER: "Wind866",
  BRANCH: "main",
  ADMIN_TOKEN: "t",
};

let n = 0;
const coldBoot = async () => (await import(`${WORKER}?x=${++n}`)).default;
const pollOf = async (mod, env) =>
  (await mod.fetch(new Request(`https://w.dev/feishu/poll?key=t`), env, { waitUntil() {} })).text();

const msg = (id, secondsAgo) => ({
  message_id: id,
  create_time: String(Date.now() - secondsAgo * 1000),
  msg_type: "text",
  sender: { sender_type: "user", id: "ou_user" },
  body: { content: JSON.stringify({ text: `@_user_1 ${CODE} 看看` }) },
  mentions: [{ key: "@_user_1", id: { open_id: "ou_bot" }, name: "png推送" }],
});

function reset() {
  store = new Map();
  counters = { receipt: 0, failed: 0, cooldown: 0, dispatch: 0 };
  items = [];
  runs = [];
  dispatchStatus = [];
  acceptedOn500 = false;
}

const cases = [];
const test = (name, fn) => cases.push([name, fn]);

test("D1 原子认领：两个实例同时扫到同一条，只回执一次、只派发一次", async () => {
  reset();
  items = [msg("om_d1", 20)];
  const db = makeDB(); // 两个实例共享同一份 D1（真实云端就是这样）
  await pollOf(await coldBoot(), { ...baseEnv, DB: db });
  const report = await pollOf(await coldBoot(), { ...baseEnv, DB: db });
  assert.equal(counters.receipt, 1, "回执应只有 1 条");
  assert.equal(counters.dispatch, 1, "派发应只有 1 次");
  assert.match(report, /D1|已被别的实例处理过/, "报告应显示被 D1 拦住");
});

test("GitHub 守卫：不依赖 D1 / Cache，也能挡住重复回执", async () => {
  reset();
  items = [msg("om_guard", 20)];
  // 云端已有一条 30 秒前的同单运行记录（另一个实例派的）
  runs = [{
    event: "workflow_dispatch",
    created_at: new Date(Date.now() - 30 * 1000).toISOString(),
    name: `个股查询 CODES=${CODE} CHAT=${CHAT}`,
    display_title: `个股查询 CODES=${CODE} CHAT=${CHAT}`,
  }];
  const report = await pollOf(await coldBoot(), baseEnv); // 故意不给 DB
  assert.equal(counters.receipt, 0, "守卫命中就不该再发「已收到」");
  assert.equal(counters.dispatch, 0, "更不该再派发");
  assert.match(report, /5 分钟内已有同单/, "报告应说明被守卫拦住");
});

test("5xx 但 GitHub 其实已受理：重试前被守卫挡住，不会出第二张图", async () => {
  reset();
  items = [msg("om_amb", 20)];
  dispatchStatus = [500];
  acceptedOn500 = true; // 500 的响应丢了，但 GitHub 已受理
  await pollOf(await coldBoot(), baseEnv);
  assert.equal(counters.dispatch, 1);
  assert.equal(counters.failed, 1, "应告诉群里这次触发失败/未知");

  // 下一个实例重试：runs 里已经有上一轮被受理的那条
  await pollOf(await coldBoot(), baseEnv);
  assert.equal(counters.dispatch, 1, "重试必须被守卫挡住，不能第二次派发");
  assert.equal(counters.receipt, 1, "回执仍然只有一条");
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
