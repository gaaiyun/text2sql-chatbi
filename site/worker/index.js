// Cloudflare Worker：静态资源由 assets 直接响应（不计入 Worker 调用）；只有 /api/* 先进入这里。
// 以后绑定自有域名或放到 Pages 后面（Service Binding 转发 /api/*）时，这份代码不用改。
//
// /api/llm 把浏览器里 SQL 智能体的请求转发给 Workers AI（账号的免费额度），返回 OpenAI chat-completions 格式。
// 防护：只接受同源请求、按 IP 限流、限制请求体大小与工具名单、服务端固定模型；页面里没有任何密钥。

const MODELS = ["@cf/zai-org/glm-4.7-flash", "@cf/qwen/qwen3-30b-a3b-fp8"];
const TOOL_NAMES = new Set(["search_schema", "describe_table", "get_column_values", "validate_sql", "preview_sql", "submit_sql"]);
const MAX_BODY_BYTES = 96_000;
const MAX_TOKENS = 1500;

const json = (data, status = 200) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });

function sameOrigin(request, url) {
  const origin = request.headers.get("origin");
  return origin === url.origin;
}

function validate(body) {
  if (!body || !Array.isArray(body.messages) || body.messages.length === 0 || body.messages.length > 40) {
    return "messages 不合法";
  }
  for (const message of body.messages) {
    if (!["system", "user", "assistant", "tool"].includes(message?.role)) return "消息角色不合法";
  }
  if (body.tools !== undefined) {
    if (!Array.isArray(body.tools) || body.tools.length > TOOL_NAMES.size) return "tools 不合法";
    for (const tool of body.tools) {
      if (!TOOL_NAMES.has(tool?.function?.name)) return `不允许的工具：${tool?.function?.name}`;
    }
  }
  return null;
}

let callCounter = 0;
const callId = () => `call_${Date.now().toString(36)}_${(callCounter++).toString(36)}`;

function normalize(result, model) {
  // Workers AI 的部分模型直接返回 OpenAI 结构，另一部分返回 { response, tool_calls }，这里统一成前者
  if (result?.choices?.[0]?.message) {
    const message = result.choices[0].message;
    return {
      model,
      choices: [
        {
          message: {
            role: "assistant",
            content: message.content ?? "",
            tool_calls: (message.tool_calls || []).map((call) => ({
              id: call.id || callId(),
              type: "function",
              function: {
                name: call.function?.name,
                arguments:
                  typeof call.function?.arguments === "string"
                    ? call.function.arguments
                    : JSON.stringify(call.function?.arguments ?? {}),
              },
            })),
          },
        },
      ],
      usage: result.usage || {},
    };
  }
  return {
    model,
    choices: [
      {
        message: {
          role: "assistant",
          content: typeof result?.response === "string" ? result.response : "",
          tool_calls: (result?.tool_calls || []).map((call) => ({
            id: callId(),
            type: "function",
            function: {
              name: call.name,
              arguments: typeof call.arguments === "string" ? call.arguments : JSON.stringify(call.arguments ?? {}),
            },
          })),
        },
      },
    ],
    usage: result?.usage || {},
  };
}

async function handleLLM(request, env, url) {
  if (request.method !== "POST") return json({ error: "只接受 POST" }, 405);
  if (!sameOrigin(request, url)) return json({ error: "只接受本站页面发起的请求" }, 403);
  if (!env.AI) return json({ error: "Workers AI 未绑定" }, 503);

  if (env.LLM_LIMITER) {
    const key = request.headers.get("cf-connecting-ip") || "anonymous";
    const { success } = await env.LLM_LIMITER.limit({ key });
    if (!success) return json({ error: "请求过于频繁，请稍后再试" }, 429);
  }

  const raw = await request.text();
  if (raw.length > MAX_BODY_BYTES) return json({ error: "请求体过大" }, 413);
  let body;
  try {
    body = JSON.parse(raw);
  } catch {
    return json({ error: "请求体不是合法 JSON" }, 400);
  }
  const problem = validate(body);
  if (problem) return json({ error: problem }, 400);

  const input = {
    messages: body.messages,
    max_tokens: Math.min(Number(body.max_tokens) || 900, MAX_TOKENS),
    temperature: Math.max(0, Math.min(Number(body.temperature ?? 0.1), 1)),
  };
  if (body.tools?.length) input.tools = body.tools;

  let lastError = null;
  for (const model of MODELS) {
    try {
      const started = Date.now();
      const result = await env.AI.run(model, input);
      const payload = normalize(result, model);
      payload.latency_ms = Date.now() - started;
      return json(payload);
    } catch (error) {
      lastError = String(error?.message || error);
      // 额度用尽时换模型也没有意义，直接返回
      if (/neuron|quota|limit exceeded|4006/i.test(lastError)) break;
    }
  }
  const exhausted = /neuron|quota|4006/i.test(lastError || "");
  return json(
    { error: exhausted ? "今日 Workers AI 免费额度已用完，请明天再试，或在本地配置模型运行" : `模型调用失败：${lastError}` },
    exhausted ? 429 : 502,
  );
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/api/health") {
      return json({ ok: true, ai: Boolean(env.AI), models: MODELS });
    }
    if (url.pathname === "/api/llm") return handleLLM(request, env, url);
    if (url.pathname.startsWith("/api/")) return json({ error: "not_found" }, 404);
    // 页面由 Pages 提供；本地 wrangler dev 带 assets 绑定时仍可直接预览
    return env.ASSETS ? env.ASSETS.fetch(request) : json({ error: "not_found" }, 404);
  },
};
