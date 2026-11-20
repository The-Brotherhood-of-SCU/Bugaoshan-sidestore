# 用 Cloudflare Worker 触发上游同步

GitHub Actions 的 `schedule` 只是「尽力而为」，实测会从 30 分钟漂到几小时。这里用
Cloudflare Worker 的 Cron Triggers（最小 1 分钟）代替它：

```
Worker Cron（每 5 分钟）
  └─ 拉上游 tags.atom → 与 KV 里的 last_tag 比较
       └─ 有新 tag → POST /repos/<ours>/dispatches（repository_dispatch）
            └─ sync.yml 的 plan → 需要时构建 preview/stable → 更新源 JSON
```

Actions 里 `sync.yml` 已经加了 `on: repository_dispatch: types: [upstream-tag]`；
原来的 `schedule` 保留为低频兜底，Worker 挂了也不会完全失联。

## 1. 创建 GitHub Token

- GitHub → Settings → Developer settings → **Fine-grained tokens** → Generate new token
- Repository access：Only select repositories → `wjj-8283/Bugaoshan-sidestore`
- Permissions → Repository permissions → **Contents: Read and write**
  （`repository_dispatch` 需要这个权限；不需要任何其他权限）
- 记下 token（`github_pat_...`），只会显示一次

## 2. 本地建 Worker 项目

```bash
mkdir bugaoshan-sync && cd bugaoshan-sync
npm init -y
npx wrangler@latest login          # 浏览器登录 Cloudflare
npx wrangler kv namespace create SYNC_STATE
# 输出里会有 id = "xxxxxxxx..."，下一步填进 wrangler.toml
```

## 3. `wrangler.toml`

```toml
name = "bugaoshan-sync"
main = "src/index.js"
compatibility_date = "2026-11-20"

[vars]
OUR_REPO = "wjj-8283/Bugaoshan-sidestore"
UPSTREAM_REPO = "The-Brotherhood-of-SCU/Bugaoshan"

[[kv_namespaces]]
binding = "SYNC_STATE"
id = "把上一步的 KV id 填这里"

[triggers]
crons = ["*/5 * * * *"]
```

## 4. `src/index.js`

```js
// bugaoshan-sync：监听上游 tag，发现变化就触发 GitHub Actions（repository_dispatch）
// /check 与 /debug 需要密码：请求头 Authorization: Bearer <key> 或 ?key=<key>
const DISPATCH_EVENT = "upstream-tag";

function latestTagFromAtom(xml) {
  const entry = xml.split("<entry>")[1] ?? "";
  const match = entry.match(/\/releases\/tag\/([^"<]+)/);
  return match ? decodeURIComponent(match[1]) : null;
}

function buildFeedUrl(env) {
  return `https://github.com/${env.UPSTREAM_REPO}/tags.atom`;
}

function secureEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) {
    return false;
  }
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

function providedKey(request) {
  const header = request.headers.get("Authorization") ?? "";
  const bearer = header.replace(/^Bearer\s+/i, "").trim();
  const explicit = (request.headers.get("X-Endpoint-Key") ?? "").trim();
  const query = (new URL(request.url).searchParams.get("key") ?? "").trim();
  return bearer || explicit || query;
}

function authorized(request, env) {
  const expected = env.ENDPOINT_KEY;
  if (!expected) {
    return false;
  }
  return secureEqual(providedKey(request), expected);
}

function unauthorizedResponse(env) {
  const error = env.ENDPOINT_KEY
    ? "unauthorized"
    : "ENDPOINT_KEY secret is not set; endpoints are disabled";
  return Response.json(
    { ok: false, error },
    { status: 401, headers: { "WWW-Authenticate": "Bearer" } },
  );
}

async function checkUpstream(env, { force = false } = {}) {
  const url = buildFeedUrl(env);

  if (!env.UPSTREAM_REPO) {
    return { ok: false, step: "config", message: "UPSTREAM_REPO 未设置", url };
  }
  if (!env.SYNC_STATE) {
    return { ok: false, step: "config", message: "KV binding SYNC_STATE 未绑上" };
  }

  let feed;
  try {
    feed = await fetch(url, {
      headers: { "User-Agent": "bugaoshan-sync-worker" },
      cf: { cacheTtl: 60 },
    });
  } catch (error) {
    return { ok: false, step: "feed", url, message: String(error) };
  }

  if (!feed.ok) {
    return {
      ok: false,
      step: "feed",
      status: feed.status,
      url,
      upstreamRepo: env.UPSTREAM_REPO ?? null,
    };
  }

  const latest = latestTagFromAtom(await feed.text());
  if (!latest) {
    return { ok: false, step: "parse", url };
  }

  const last = await env.SYNC_STATE.get("last_tag");
  if (!force && last === latest) {
    return { ok: true, latest, last, dispatched: false };
  }

  if (!env.OUR_REPO) {
    return { ok: false, step: "dispatch", message: "OUR_REPO 未设置", latest, last };
  }
  if (!env.GITHUB_TOKEN) {
    return { ok: false, step: "dispatch", message: "GITHUB_TOKEN 未设置", latest, last };
  }

  const response = await fetch(
    `https://api.github.com/repos/${env.OUR_REPO}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "bugaoshan-sync-worker",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        event_type: DISPATCH_EVENT,
        client_payload: { tag: latest, source: "cloudflare-worker" },
      }),
    },
  );

  if (!response.ok) {
    return {
      ok: false,
      step: "dispatch",
      status: response.status,
      body: await response.text(),
      latest,
      last,
      ourRepo: env.OUR_REPO,
    };
  }

  try {
    await env.SYNC_STATE.put("last_tag", latest);
  } catch (error) {
    // 写失败也不能让检查整体抛错，否则下一分钟会重复 dispatch
    console.log("[last_tag]", String(error));
  }
  return { ok: true, latest, last, dispatched: true };
}

async function recordStatus(env, result) {
  if (!env.SYNC_STATE) {
    return;
  }
  try {
    const now = Date.now();
    const raw = await env.SYNC_STATE.get("last_check");
    const previous = raw ? JSON.parse(raw) : null;
    const lastAt = previous && previous.at ? Date.parse(previous.at) : 0;
    const important = result.dispatched === true || result.ok !== true;

    // 每分钟检查时，普通结果最多 15 分钟写一次，避免打满免费版 KV 写入额度（1,000/天）；
    // dispatch 和失败立即写，方便排查。
    if (!important && now - lastAt < 15 * 60 * 1000) {
      return;
    }

    await env.SYNC_STATE.put(
      "last_check",
      JSON.stringify({ at: new Date().toISOString(), ...result }),
      { expirationTtl: 7 * 24 * 3600 },
    );
  } catch (error) {
    console.log("[recordStatus]", String(error));
  }
}

async function debugInfo(env) {
  const info = {
    now: new Date().toISOString(),
    upstreamRepo: env.UPSTREAM_REPO ?? null,
    ourRepo: env.OUR_REPO ?? null,
    hasToken: Boolean(env.GITHUB_TOKEN),
    hasEndpointKey: Boolean(env.ENDPOINT_KEY),
    hasKV: Boolean(env.SYNC_STATE),
    feedUrl: buildFeedUrl(env),
    feedStatus: null,
    parsedLatestTag: null,
    lastTagInKV: null,
    lastCheck: null,
    feedError: null,
    kvError: null,
  };

  if (!env.UPSTREAM_REPO) {
    return info;
  }

  try {
    const feed = await fetch(info.feedUrl, {
      headers: { "User-Agent": "bugaoshan-sync-worker" },
    });
    info.feedStatus = feed.status;
    if (feed.ok) {
      info.parsedLatestTag = latestTagFromAtom(await feed.text());
    }
  } catch (error) {
    info.feedError = String(error);
  }

  if (env.SYNC_STATE) {
    try {
      info.lastTagInKV = await env.SYNC_STATE.get("last_tag");
      const raw = await env.SYNC_STATE.get("last_check");
      info.lastCheck = raw ? JSON.parse(raw) : null;
    } catch (error) {
      info.kvError = String(error);
    }
  }

  return info;
}

export default {
  // Cloudflare Cron Trigger 每 5 分钟调用一次（不需要密码）
  async scheduled(event, env, ctx) {
    ctx.waitUntil(
      checkUpstream(env).then(async (result) => {
        console.log("[check]", JSON.stringify(result));
        await recordStatus(env, result);
      }),
    );
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    const isProtected = url.pathname === "/check" || url.pathname === "/debug";

    if (isProtected && !authorized(request, env)) {
      return unauthorizedResponse(env);
    }

    if (url.pathname === "/debug") {
      return Response.json(await debugInfo(env));
    }

    if (url.pathname === "/check") {
      const force = url.searchParams.get("force") === "1";
      const result = await checkUpstream(env, { force });
      console.log("[check]", JSON.stringify(result));
      await recordStatus(env, result);
      return Response.json(result, { status: result.ok ? 200 : 500 });
    }

    return new Response(
      "bugaoshan sync worker\n\nGET /check?key=...&force=1\nGET /debug?key=...\n",
      { status: 200 },
    );
  },
};
```

## 5. 配置 secret 并部署

```bash
npx wrangler secret put GITHUB_TOKEN   # 粘贴第 1 步的 PAT
npx wrangler secret put ENDPOINT_KEY   # 随便生成一串长随机值，/check 与 /debug 的访问密码
npx wrangler deploy
```

`ENDPOINT_KEY` 用来保护 `/check` 和 `/debug`（cron 内部调用不需要）。生成方式例如
`openssl rand -hex 24`；不设置它时两个端点会返回 401 并提示 secret 未配置。

部署输出里会有 Worker URL，形如
`https://bugaoshan-sync.<你的子域>.workers.dev`。

## 6. 验证

```bash
# 必须带密码：推荐 Authorization 头，避免 key 出现在 URL/日志里
curl -s -H "Authorization: Bearer <ENDPOINT_KEY>" \
  https://bugaoshan-sync.<子域>.workers.dev/check | jq
# {"ok":true,"latest":"v2.6.1-preview.6","last":null,"dispatched":true}

# 浏览器调试也可以用查询参数
open "https://bugaoshan-sync.<子域>.workers.dev/debug?key=<ENDPOINT_KEY>"

# 再看 Actions 是否被触发（event 应为 repository_dispatch）
gh run list --limit 3 --json event,displayTitle,status --jq '.[] | "\(.event) \(.displayTitle) \(.status)"'
```

返回 `dispatched:false` 表示上游没有新 tag；`?force=1` 可以强制触发一次做联调。

## 7. 注意事项

- 免费计划包含 Cron Triggers（1 分钟粒度，额度以 Cloudflare 控制台为准）。注意免费版
  KV 写入额度是 **1,000/天**：代码里的 `recordStatus` 对普通结果做了 15 分钟节流，
  dispatch / 失败才立即写，因此即使 `* * * * *` 每分钟检查，写入也远低于额度。
- KV 读取有最终一致性（约 60 秒），5 分钟粒度下不会重复触发；即使重复也无害——
  Actions 的 plan 会发现没有新版本而跳过，且有 `concurrency: sync-upstream` 串行保护。
- Worker 只读取公开的 Atom feed，并调用我们仓库的 dispatch API；不接触任何用户数据。
- PAT 只放在 Worker Secret 里，不要提交进仓库；建议 90 天轮换一次。
- `ENDPOINT_KEY` 泄露后重新 `secret put` 覆盖即可；查询参数 `?key=` 会进 URL 和日志，优先用 Authorization 头。
- 想暂停自动化：Cloudflare 控制台删掉 Cron Trigger（或删 Worker）；Actions 侧还有低频 cron 兜底。

## 8. 排错

| 现象 | 排查 |
| --- | --- |
| `/check` 返回 401/403 | PAT 过期/权限不足；确认是 `Contents: Read and write` 且只授权本仓库 |
| `/check` 返回 500 且 `step=feed` | Cloudflare 到 github.com 的请求失败，重试或稍后再看 |
| dispatch 成功（`ok:true`）但 Actions 没跑 | `sync.yml` 的 `repository_dispatch` 触发器是否已推送；token 是否能写 Actions/Contents |
| Actions 跑了但立刻成功跳过 | 说明源 JSON 已是最新 tag，属正常 |
| 日志排查 | `npx wrangler tail bugaoshan-sync` |
