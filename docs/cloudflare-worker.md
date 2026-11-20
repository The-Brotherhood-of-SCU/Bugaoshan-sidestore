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
const DISPATCH_EVENT = "upstream-tag";

function latestTagFromAtom(xml) {
  // tags.atom 的第一条 entry 就是最新 tag；从 /releases/tag/<tag> 里取
  const entry = xml.split("<entry>")[1] ?? "";
  const match = entry.match(/\/releases\/tag\/([^"<]+)/);
  return match ? decodeURIComponent(match[1]) : null;
}

async function checkUpstream(env, { force = false } = {}) {
  const feed = await fetch(`https://github.com/${env.UPSTREAM_REPO}/tags.atom`, {
    headers: { "User-Agent": "bugaoshan-sync-worker" },
    cf: { cacheTtl: 60 },
  });
  if (!feed.ok) {
    return { ok: false, step: "feed", status: feed.status };
  }

  const latest = latestTagFromAtom(await feed.text());
  if (!latest) {
    return { ok: false, step: "parse" };
  }

  const last = await env.SYNC_STATE.get("last_tag");
  if (!force && last === latest) {
    return { ok: true, latest, last, dispatched: false };
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
        client_payload: { tag: latest },
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
    };
  }

  await env.SYNC_STATE.put("last_tag", latest);
  return { ok: true, latest, last, dispatched: true };
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(checkUpstream(env));
  },

  // 手动测试：GET /check（?force=1 强制再触发一次）
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/check") {
      const force = url.searchParams.get("force") === "1";
      const result = await checkUpstream(env, { force });
      return Response.json(result, { status: result.ok ? 200 : 500 });
    }
    return new Response("bugaoshan sync worker: GET /check", { status: 200 });
  },
};
```

## 5. 配置 secret 并部署

```bash
npx wrangler secret put GITHUB_TOKEN   # 粘贴第 1 步的 PAT
npx wrangler deploy
```

部署输出里会有 Worker URL，形如
`https://bugaoshan-sync.<你的子域>.workers.dev`。

## 6. 验证

```bash
curl -s https://bugaoshan-sync.<子域>.workers.dev/check | jq
# {"ok":true,"latest":"v2.6.1-preview.6","last":null,"dispatched":true}

# 再看 Actions 是否被触发（event 应为 repository_dispatch）
gh run list --limit 3 --json event,displayTitle,status --jq '.[] | "\(.event) \(.displayTitle) \(.status)"'
```

返回 `dispatched:false` 表示上游没有新 tag；`?force=1` 可以强制触发一次做联调。

## 7. 注意事项

- 免费计划包含 Cron Triggers（1 分钟粒度，额度以 Cloudflare 控制台为准）；
  `*/5` 一天约 288 次，远低于免费额度。
- KV 读取有最终一致性（约 60 秒），5 分钟粒度下不会重复触发；即使重复也无害——
  Actions 的 plan 会发现没有新版本而跳过，且有 `concurrency: sync-upstream` 串行保护。
- Worker 只读取公开的 Atom feed，并调用我们仓库的 dispatch API；不接触任何用户数据。
- PAT 只放在 Worker Secret 里，不要提交进仓库；建议 90 天轮换一次。
- 想暂停自动化：Cloudflare 控制台删掉 Cron Trigger（或删 Worker）；Actions 侧还有低频 cron 兜底。

## 8. 排错

| 现象 | 排查 |
| --- | --- |
| `/check` 返回 401/403 | PAT 过期/权限不足；确认是 `Contents: Read and write` 且只授权本仓库 |
| `/check` 返回 500 且 `step=feed` | Cloudflare 到 github.com 的请求失败，重试或稍后再看 |
| dispatch 成功（`ok:true`）但 Actions 没跑 | `sync.yml` 的 `repository_dispatch` 触发器是否已推送；token 是否能写 Actions/Contents |
| Actions 跑了但立刻成功跳过 | 说明源 JSON 已是最新 tag，属正常 |
| 日志排查 | `npx wrangler tail bugaoshan-sync` |
