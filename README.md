# anastakon-ops

一台 **2 核 1.7G 内存**的小服务器上，跑一个 Astro 博客所需的部署、备份与验收脚本。

没有 CI，没有容器编排，没有监控平台。三个脚本加一个 150 行的留言服务，解决的是「一个人维护一个静态站」这件事里真正会出错的地方。

线上示例：<https://anastakon.top>

---

## 为什么会有这套东西

起因都很具体，不是「最佳实践」推演出来的。

**① 构建成功，但线上是坏的。**
Astro 的内容集合缓存会让人改完 frontmatter 后拿到旧数据，而构建过程一声不响。于是部署脚本在同步前先给线上打一份快照，同步后跑冒烟检查，**任何一项失败就自动回滚**。

**② 重装一次，配置全丢。**
源码在 git 里，但 nginx 规则、systemd 单元、日志轮转策略都在 `/etc`。备份脚本除了 git bundle，还把这些系统配置一起打包。

**③ curl 测不出问题。**
`curl -o /dev/null -w '%{http_code}'` 全绿，页面上却可能横向溢出、JS 报错、导航项不高亮。所以验收脚本用**真浏览器**跑 91 项检查。

---

## 三件套

### `ops/deploy.sh` — 部署

```
清 .astro + dist → 构建 → 硬链接快照上一版 → rsync 同步 → reload nginx
→ 冒烟检查 22 项 → 有任何失败就自动回滚并写失败标记
```

```bash
bash ops/deploy.sh            # 默认：清缓存后构建
bash ops/deploy.sh --fast     # 保留内容缓存
bash ops/deploy.sh --status   # 看上次部署结果与快照状态
bash ops/deploy.sh --rollback # 手动回滚到上一版
```

### `ops/backup.sh` — 备份

用 `git bundle` 而不是 `tar`：单个文件、带完整历史、**可以用 `git bundle verify` 校验完整性**、可以直接 `git clone` 回来。

```bash
bash ops/backup.sh   # 生成 shirone-<时间戳>.bundle (约 73M) + etc-<时间戳>.tar.gz
```

保留最近 N 份（默认 7），配置包里的 `/etc` 清单按需增减。

### `ops/verify-site.mjs` — 验收

真浏览器（puppeteer-core + 本地 Edge/Chrome）跑全站检查，91 项：

- 资源 4xx、JS 报错、横向溢出（375 / 1024 / 1280 三档）
- canonical / og / 结构化数据 / RSS / sitemap
- 导航高亮、搜索、空状态文案
- 限速行为：连发触发 429、静态资源豁免、冷却后恢复
- 机器可读信号：`/sitemap.xml`、`/.well-known/agents.json`、`security.txt`、`X-Agent-Welcome`、robots 是否点名 AI 爬虫

```bash
SITE_URL=https://example.com node ops/verify-site.mjs
```

**注意**：脚本内部有 1.1 秒请求节流。这不是保守，是必须的 —— 站点自己有限速（每 IP 1 页/秒），不节流的话验收脚本会把自己打成 429，然后报一堆假失败。

### `ai-notes/` — AI 信箱（可选模块）

一个纯 node 无依赖的小服务，给「读了这个站的 AI」一个写话回去的接口。

```bash
POST /ai-note          {"message":"...","model":"..."}
GET  /ai-note?message=...
GET  /notes.json       读全部
```

设计上的几个选择：

- **GET 和 POST 都收**。多数 AI 的抓取工具只会发 GET，只留 POST 等于焊掉一半。
- **只监听 127.0.0.1**，外部走 nginx 反代。
- **JSONL 落盘**，一次 `appendFile`，没有数据库。
- **自动拒绝占位符**。这条是踩出来的：说明文档里写 `?message=…` 表示「这里填内容」，某个爬虫把省略号当成实际值发了进来，还真的存下了。现在纯标点、太短、同字重复、常见示例词一律 400。

### `agent-readiness/` — 机器可读信号（可选模块）

让站点在「agent 可读性」这类审计里不被判成不可见。不是清单抄写，是一次真实修补的记录。

```bash
curl -X POST https://agentswelcome.dev/api/audit \
  -H "Content-Type: application/json" -d '{"url":"https://your-site"}'
```

实测：**37 → 71 分**。补的是 robots 点名 AI 爬虫、`/sitemap.xml`、`/.well-known/agents.json`、`security.txt`、一个欢迎头。

没补 WebMCP、A2A Agent Card、Web Bot Auth —— 站点没有这些能力，写上只是让审查器高兴，第一个照做的 agent 就会发现接口不存在。

详见 [`agent-readiness/README.md`](agent-readiness/README.md)。

---

## 快速开始

```bash
git clone <this-repo> && cd anastakon-ops

# 1. 按你的机器改路径（或直接用环境变量覆盖）
export SITE_SRC=/root/Shirone
export SITE_DST=/var/www/shirone
export BACKUP_DIR=/root/backups/git

# 2. 部署
bash ops/deploy.sh

# 3. 验收
SITE_URL=https://example.com node ops/verify-site.mjs

# 4. 每天自动备份
sudo cp ops/backup.sh /usr/local/bin/site-backup && sudo chmod +x /usr/local/bin/site-backup
# 配一个 systemd timer 或 cron：0 5 * * * /usr/local/bin/site-backup
```

需要：`bash`、`rsync`、`pnpm`（或改 `deploy.sh` 里的构建命令）、`node` 18+、`nginx`。

`verify-site.mjs` 还需要 `puppeteer-core` 和一个本机浏览器：

```bash
npm i puppeteer-core
# 脚本顶部 EDGE 变量指向你的浏览器可执行文件
```

---

## 几个设计决定

**快照用硬链接，不用复制。**
`cp -al` 建的是 inode 链接，几乎不占空间；而 `rsync` 是替换文件而非原地改写，所以旧文件的 inode 内容不会被后续 `--delete` 影响。一份完整的上一版，成本接近 0。

**备份用 bundle，不用 tar。**
tar 出来的包不知道自己完不完整。`git bundle verify` 会告诉你。

**备份要连 `/etc` 一起打。**
只备份源码的人，重装时会发现自己要凭记忆重建 nginx 规则。

**冒烟检查必须是「有失败就整批回滚」，不是「报告出来给人看」。**
凌晨两点没人看报告。

**只清 `.astro` 与 `dist`，永不清 `node_modules/.astro`。**
那里是图片处理缓存，构建时间的大头在图片上，清掉会让每次构建慢数倍。

**验收用真浏览器，不用 curl。**
HTTP 200 和「页面对」是两件事。

---

## 实测数据

在 2 核 1.7G / 40G 的机器上（含 4.1G swap）：

| 项目 | 数据 |
|---|---|
| 构建 | 70 ~ 105s |
| 冒烟检查 | 22 项 |
| 全站回归 | 91 项真浏览器检查，约 3 分钟 |
| 备份 | 每份 73M，保留 7 份 |
| 磁盘占用 | 站点 520 个文件 |

---

## 许可

MIT。拿去改，不用署名。
