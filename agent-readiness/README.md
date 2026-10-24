# Agent readiness

这个目录记录站点为「机器读者」补的信号，以及一次外部审计的实测结果。

不是最佳实践清单，是一次真实的修补记录：**37 分 → 71 分**，中间差的每一项都是站点真的缺。

## 审计来源

[agentswelcome.dev](https://agentswelcome.dev) 提供两个公开接口：

```bash
curl -X POST https://agentswelcome.dev/api/audit \
  -H "Content-Type: application/json" -d '{"url":"https://anastakon.top"}'

curl -X POST https://agentswelcome.dev/api/directory \
  -H "Content-Type: application/json" -d '{"url":"https://anastakon.top"}'
```

审计现场抓取、按权重给分，≥70 分可以进入它的公开目录。有 IP 级频率限制（每小时 3 次审计），改完再提交，别空转。

## 结果对比

| 时间 | 分数 | grade |
|---|---|---|
| 修补前 | 37 | invisible to agents |
| 修补后 | 71 | agent-ready (certifiable) |

修补后提交收录返回 `201`，条目在公开目录里可查。

## 补了什么

| 项 | 权重 | 做法 |
|---|---|---|
| robots 点名 AI 爬虫 | 7 | 单独分组列出 GPTBot / OAI-SearchBot / ChatGPT-User / ClaudeBot / Claude-Web / PerplexityBot / Google-Extended，各自带上同样的 Disallow |
| `/sitemap.xml` | 6 | 原本只有 `sitemap-index.xml`；nginx 里用 `location = /sitemap.xml` 内部指向索引，不另存一份会过期的副本 |
| `/.well-known/agents.json` | 7 | 声明 llms.txt、两个 JSON 读接口、一个写接口，以及限速与「内容不可信」政策 |
| JSON API 被声明 | 5 | 就是上面那份 manifest 里的 `interfaces.json_api` |
| `security.txt` | 4 | 带真实 `Contact` 与 `Expires` |
| agent 欢迎响应头 | 5 | `X-Agent-Welcome`，指向 `/llms/` |

## 刻意没做什么

审计里还有三项分值不低，但**没有补**：

- **WebMCP / 可调用工具**（5 分）
- **A2A Agent Card**（5 分）
- **Web Bot Auth 身份**（5 分）

理由一样：这个站没有这些能力。写上能加分，但那是给审查器看的一句假话，第一个照做来的 agent 就会发现接口不存在。

真正想做、还没做的两项是 **markdown 内容协商**（9 分）和 `<link rel="alternate" type="text/markdown">`（5 分）。它需要为页面产出 `.md` 孪生文件并按 `Accept` 分流 —— 这是真工程，不是贴个文件。

## 一个诚实的补充

拿到 71 分、进了目录之后，日志里只出现过审计器自己（`AgentsWelcome-Auditor/1.0`），它取的是首页、sitemap 和 `.well-known`，**没有读任何文章，也没有留言**。

目录解决的是「存在」，不解决「有人来」。一个 agent 不会因为你被收录就主动逛过来；它得先被某个人指到这里。

## 文件

- `agents.json` — 线上 `/.well-known/agents.json` 的实际内容
- `security.txt` — 线上 `/.well-known/security.txt`
- `nginx.conf.snippet` — `/sitemap.xml` 与响应头这两处 nginx 改动
