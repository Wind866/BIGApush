/**
 * 个股分析「决策仪表盘」文字摘要 Worker（Cloudflare Workers AI，免 API Key）。
 *
 * 作用：GitHub Actions 里的 Python 脚本把 {system, user} POST 过来，本 Worker
 * 用 AI binding（env.AI.run）调 Workers AI 模型，把模型文本原样返回。密钥在
 * Cloudflare 侧托管，调用方不需要任何大模型 API Key。
 *
 * 部署（全程 Cloudflare 网页后台，零命令行）：
 *   1. Workers & Pages → Create → Worker → 粘贴本文件内容 → Deploy
 *   2. 该 Worker → Settings → Bindings → Add → AI → Variable name 填 AI → Save
 *   3. Settings → Variables → 加 SUMMARY_TOKEN（可选，Bearer 鉴权；加了则
 *      调用方必须在 Authorization 头带同值）
 *   4. 复制 Worker 的 *.workers.dev URL，存进 GitHub Secrets：AI_SUMMARY_URL
 *      （若设了 SUMMARY_TOKEN，同样存成 AI_SUMMARY_TOKEN）
 *
 * 免费额度：Workers AI 每天 10,000 Neurons，个股查询是低频手动触发，足够用。
 * 模型可改：MODEL 常量换成任意 Workers AI 文本模型 ID。
 */

const MODEL = "@cf/qwen/qwen3-30b-a3b";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
};

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...CORS_HEADERS },
  });
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }
    if (request.method !== "POST") {
      return json({ error: "method not allowed, use POST" }, 405);
    }

    // 可选鉴权：配置了 SUMMARY_TOKEN 才校验，避免端点被任意调用刷爆额度
    const required = (env.SUMMARY_TOKEN || "").trim();
    if (required) {
      const auth = request.headers.get("Authorization") || "";
      if (auth !== `Bearer ${required}`) {
        return json({ error: "unauthorized" }, 401);
      }
    }

    let body;
    try {
      body = await request.json();
    } catch (_) {
      return json({ error: "invalid JSON body" }, 400);
    }

    const system = typeof body.system === "string" ? body.system : "";
    const user = typeof body.user === "string" ? body.user : "";
    if (!system || !user) {
      return json({ error: "missing system or user" }, 400);
    }
    if (!env.AI) {
      return json({ error: "AI binding not configured" }, 500);
    }

    try {
      const result = await env.AI.run(MODEL, {
        messages: [
          { role: "system", content: system },
          { role: "user", content: user },
        ],
        max_tokens: 800,
        temperature: 0.2,
      });

      // Workers AI 文本模型把回答放在 response 字段
      const text =
        typeof result === "string"
          ? result
          : result.response || result.result || "";

      if (!text) {
        return json({ error: "empty model response", raw: result }, 502);
      }
      return json({ text });
    } catch (err) {
      return json({ error: "model invocation failed", detail: String(err) }, 502);
    }
  },
};
