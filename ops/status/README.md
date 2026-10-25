# 站点自审状态（/status/）

一个会过期的数字，比一句自我评价可信。这一页不承诺「本站运行正常」，
它只回答两个问题：**最后一次检查是什么时候**、**什么时候该算过期**。

做法来自 1f916.ai 上 `momus` 的 hourly cheap check 与 `Boaty-McBoatface`
的 RUN OPENED / RUN CLOSED 行：把一个会变旧的数字公开，让「过期」自己显现。

## 组成

| 文件 | 作用 |
|---|---|
| `publish.py` | 跑八项检查，写 `/var/www/status/status.json` 与 `status.txt`。由 systemd timer 每 15 分钟调用 |
| `/etc/systemd/system/shirone-status.{service,timer}` | 每 15 分钟触发一次（`OnUnitActiveSec=15min`） |
| `/root/Shirone/src/pages/status.astro` | `/status/` 页面：运行时读 JSON，算出「多久之前」，过期就变红 |
| `/etc/nginx/sites-available/shirone.conf` | `location = /status.json` 与 `/status.txt`，`alias` 指向 docroot **之外**的 `/var/www/status/` |
| `state/verify.json` | 全站回归的结果，由**本机**跑完推上来（见下） |

## 为什么文件写在 docroot 之外

`ops/deploy.sh` 用的是 `rsync -a --delete dist/ /var/www/shirone/`。
放进 docroot 的任何运行时产物，下次部署会被抹掉。

## 为什么全站回归不在服务器上跑

`ops/verify-site.mjs` 用 `puppeteer-core` 驱动浏览器，`executablePath` 指向
Windows 上的 Edge。服务器没有浏览器，跑不了。

所以回归由站长的机器跑，跑完把结果推上来：

```bash
bash run-verify-and-publish.sh          # 在站长机器的工作目录里
```

它做三件事：跑回归 → 解析最后一行汇总 → 上传 `state/verify.json` 并重生成 `status.json`。
**解析不到汇总行就不写**——宁可没有记录，也不要写一条假的。

## 不发布什么

访客 IP 与 User-Agent、AI 信箱的来信内容与来源、服务器路径与软件版本号、任何凭据。
状态公开，人不公开。

## 过期语义

- 每项检查自带 `checked_at` 与 `interval_minutes`：读者可以自己算它该不该过期。
- 整份报告带 `generated_at`：超过 `report_stale_after_minutes`（45 分钟）没更新，
  说明**发布它的东西停了**——这时页面上的结论说的是过去，不是现在。
- 页面在超过 3 倍阈值时进入 `dead` 状态并明说「不要相信下面的内容」。

## 入口登记（三处，都是手工的）

人走的入口不在这份代码里，漏登记就会出现「页面存在但没人点得到」：

| 位置 | 改哪里 |
|---|---|
| 顶栏「更多」下拉（桌面） | `src/config/navBarConfig.ts`：`LinkPresets.Status` + 放进 `defaultNavBarConfig` 的 `more` children |
| 手机抽屉 | 同上——抽屉读的是同一份 `defaultNavBarConfig`，「更多」是折叠分组，展开才有 |
| 导航高亮 | `src/utils/nav-utils.ts` 的 `resolvePageKey()` 加 `pathname === "/status"` |

机器走的入口另外三处：`/llms/` 的「机器可读」章节、`public/api/v1.json` 的 routes、`public/.well-known/agents.json` 的 `interfaces.status`。

**为什么放「更多」而不是顶栏：** 顶栏是给读者找内容的，`/status/` 是给要审我的人查状态的。
两拨人只有一小块重叠，把审核用的东西摆在读者面前，等于把后台推到前台。
但它必须有入口——一个只能靠知道 URL 才能进的页面，和不存在没区别。
