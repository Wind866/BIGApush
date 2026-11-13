/**
 * Cloudflare Worker · GitHub Actions 准点触发器
 *
 * 背景：GitHub 的 schedule 派发不可靠。BIGApush 实测 33 次定时运行 0 次准时，
 * 延迟 3h17m ~ 9h43m（延迟发生在调度器侧，不是 runner 排队：run 创建 → job 启动仅 3~4 秒）。
 *
 * 这个 Worker 负责在准点调用 workflow_dispatch，仓库里的 cron 退化成兜底。
 * 两个触发器会不会把同一条新闻推两次？不会——脚本侧有 utils/push_guard.py 幂等守卫。
 *
 * 环境变量（在 CF 控制台 Variables and Secrets 里配）：
 *   GH_TOKEN   Secret，fine-grained PAT，只有目标仓库的 Actions: Read and write
 *   REPO       例如 Wind866/BIGApush
 *   WORKFLOWS  逗号分隔的 workflow 文件名，例如 evening-news.yml
 *   BRANCH     默认 main
 */

const API = "https://api.github.com";

async function dispatchOne(env, workflow) {
  const url = `${API}/repos/${env.REPO}/actions/workflows/${workflow}/dispatches`;
  let last = "";

  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GH_TOKEN}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "biga-push-cron",
        },
        body: JSON.stringify({ ref: env.BRANCH || "main" }),
      });

      // 成功是 204 No Content
      if (res.status === 204) return `✅ ${workflow} 已触发`;

      const body = (await res.text()).slice(0, 300);
      last = `❌ ${workflow} HTTP ${res.status} ${body}`;
      // 401/403/404 是配置问题（token 权限、仓库名、文件名），重试没有意义
      if (res.status < 500) return last;
    } catch (err) {
      last = `❌ ${workflow} 请求异常 ${err}`;
    }
    if (attempt < 3) await new Promise((r) => setTimeout(r, 800 * attempt));
  }
  return last;
}

async function dispatchAll(env, source) {
  if (!env.GH_TOKEN) return "❌ 未配置 GH_TOKEN";
  if (!env.REPO) return "❌ 未配置 REPO";

  const list = (env.WORKFLOWS || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  if (!list.length) return "❌ 未配置 WORKFLOWS";

  const lines = [`[${source}] ${new Date().toISOString()} 触发 ${list.length} 个 workflow`];
  let failed = 0;
  for (const wf of list) {
    const out = await dispatchOne(env, wf);
    lines.push(out);
    if (out.startsWith("❌")) failed++;
  }
  const text = lines.join("\n");
  console.log(text);

  // 有失败就抛出，让 CF 的调用记录标红，便于在 Metrics 里看到
  if (failed) throw new Error(text);
  return text;
}

export default {
  // 定时触发
  async scheduled(event, env, ctx) {
    await dispatchAll(env, `cron ${event.cron}`);
  },

  // 手动触发：浏览器打开 Worker 域名就是跑一次（用于配置后自测）
  async fetch(req, env) {
    const url = new URL(req.url);
    if (url.pathname === "/health") return new Response("ok", { status: 200 });
    try {
      const text = await dispatchAll(env, "manual");
      return new Response(text + "\n", {
        status: 200,
        headers: { "content-type": "text/plain; charset=utf-8" },
      });
    } catch (err) {
      return new Response(String(err) + "\n", {
        status: 500,
        headers: { "content-type": "text/plain; charset=utf-8" },
      });
    }
  },
};
