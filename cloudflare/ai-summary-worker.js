/**
 * 个股分析「决策仪表盘」文字摘要 Worker（Cloudflare Workers AI，免 API Key）。
 *
 * 作用：GitHub Actions 里的 Python 脚本把 {system, user} POST 过来，本 Worker
 * 用 AI binding（env.AI.run）调 Workers AI 模型，把模型文本原样返回。密钥在
 * Cloudflare 侧托管，调用方不需要任何大模型 API Key。
 *
 * 部署（全程 Cloudflare 网页后台，零命令行）：
 *   1. Workers & Pages → Create → Worker → 粘贴本文件内容 → Deploy
 *   2. 该 Worker → Settings → Bindings → Add binding → Workers AI → Variable name 填 AI → Save
 *      （列表里选带星标的 "Workers AI"，不是 "AI Search"；变量名必须是 AI）
 *   3. Settings → Variables → 加 SUMMARY_TOKEN（可选，Bearer 鉴权；加了则
 *      调用方必须在 Authorization 头带同值）
 *   4. 复制 Worker 的 *.workers.dev URL，存进 GitHub Secrets：AI_SUMMARY_URL
 *      （若设了 SUMMARY_TOKEN，同样存成 AI_SUMMARY_TOKEN）
 *
 * 免费额度：Workers AI 每天 10,000 Neurons，个股查询是低频手动触发，足够用。
 * 模型可改：MODEL 常量换成任意 Workers AI 文本模型 ID。
 */

// 主模型：智谱 GLM-4.7-Flash（中文好、无下线计划、支持 response_format json_object）。
// 旧写法 @cf/qwen/qwen3-30b-a3b 已不存在（正确带 -fp8 且 2026-11-20 下线），故弃用。
// MODELS 按顺序尝试，某个模型下线/报错会自动换下一个，避免整条链路因单模型失效而断。
const MODELS = [
  "@cf/zai-org/glm-4.7-flash",
  "@cf/qwen/qwen3-30b-a3b-fp8",
  "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
];

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

    // 依次尝试各模型：任一下线/报错/返回空，自动换下一个。
    let lastErr = "unknown";
    for (const model of MODELS) {
      try {
        const inputs = {
          messages: [
            { role: "system", content: system },
            { role: "user", content: user },
          ],
          max_tokens: 900,
          temperature: 0.2,
        };
        // GLM 支持 response_format，强制 JSON 输出，便于 Python 端稳定解析四段文字。
        if (model.startsWith("@cf/zai-org/")) {
          inputs.response_format = { type: "json_object" };
        }

        const result = await env.AI.run(model, inputs);

        // 不同模型返回字段不一：response / result，OpenAI 兼容模型在 choices[0].message.content。
        let text =
          typeof result === "string"
            ? result
            : result.response || result.result || "";
        if (!text && result && Array.isArray(result.choices)) {
          const msg = result.choices[0] && result.choices[0].message;
          text = (msg && msg.content) || "";
        }

        if (text) {
          return json({ text, model });
        }
        lastErr = "empty response from " + model;
      } catch (err) {
        lastErr = model + ": " + String(err);
      }
    }
    return json({ error: "all models failed", detail: lastErr }, 502);
  },
};
