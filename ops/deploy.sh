#!/usr/bin/env bash
# Shirone 一键更新：快照 -> 构建 -> 同步 -> 重载 Nginx -> 冒烟检查 ->（失败自动回滚）
#
# 用法：
#   bash /root/deploy.sh            默认：清 .astro + dist 后构建（干净，实测 77s）
#   bash /root/deploy.sh --fast     保留 .astro 内容缓存（实测不比默认快；仅在网络不通、
#                                   不想让构建去重抓字体元数据时用）
#   bash /root/deploy.sh --status   看最近一次部署结果与快照状态
#   bash /root/deploy.sh --rollback 手动回滚到上一版（冒烟失败时脚本已自动回滚）
#
# 三条铁律：
#   1. 只清 .astro 与 dist，**绝不清 node_modules/.astro** —— 那里是图片处理缓存，
#      约 77s 的构建时间大头就在图片上，清掉会让每次构建慢数倍。
#   2. 改过文章 frontmatter / 内容集合定义 / siteConfig 后必须用默认档（本来也是默认）。
#   3. 同步前必须先打快照，否则「构建成功但内容坏了」时没有可回退的版本。
set -euo pipefail

# 路径按你的实际部署位置改，或用环境变量覆盖
SRC="${SITE_SRC:-/root/Shirone}"
DST="${SITE_DST:-/var/www/shirone}"
PREV="$(dirname "$DST")/$(basename "$DST").prev"
STATE="${DEPLOY_STATE:-/root/.deploy_state}"
FAILMARK="${DEPLOY_FAILMARK:-/root/DEPLOY_FAILED}"
CLEAN=1
ACTION="deploy"

for arg in "$@"; do
  case "$arg" in
    --fast|-f) CLEAN=0 ;;
    --clean|-c) CLEAN=1 ;;
    --rollback) ACTION="rollback" ;;
    --status) ACTION="status" ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "未知参数: $arg（可用 --fast / --clean / --rollback / --status）" >&2; exit 2 ;;
  esac
done

# ---------- --status ----------
if [ "$ACTION" = "status" ]; then
  if [ -f "$STATE" ]; then
    echo "最近一次：$(cat "$STATE")"
  else
    echo "最近一次：无记录（还没用新版脚本跑过）"
  fi
  if [ -d "$PREV" ]; then
    echo "可回滚快照：$PREV（$(find "$PREV" -type f | wc -l) 个文件，$(du -sh "$PREV" | cut -f1)）"
  else
    echo "可回滚快照：无"
  fi
  if [ -f "$FAILMARK" ]; then
    echo "--- 未清除的失败标记 ---"
    cat "$FAILMARK"
  fi
  exit 0
fi

# ---------- --rollback ----------
if [ "$ACTION" = "rollback" ]; then
  if [ ! -d "$PREV" ]; then
    echo "没有可回滚的快照：$PREV 不存在" >&2
    exit 1
  fi
  echo "回滚 $PREV -> $DST"
  rsync -a --delete --exclude '.user.ini' "$PREV/" "$DST/"
  nginx -t && systemctl reload nginx
  echo "OK $(date '+%F %T') 手动回滚" > "$STATE"
  rm -f "$FAILMARK"
  echo "已回滚到上一版（$(date '+%F %T')）"
  exit 0
fi

echo "[1/6] 源码目录 $SRC"
cd "$SRC"

if [ "$CLEAN" = 1 ]; then
  rm -rf .astro dist
  echo "[2/6] 已清 .astro 与 dist（node_modules/.astro 图片缓存保留）"
else
  rm -rf dist
  echo "[2/6] --fast：保留 .astro 内容缓存"
fi

# 可选：构建期 CDN 重定向钩子。服务器访问 cdn.jsdelivr.net 被 SNI 阻断时，
# 把字体元数据的请求换到镜像域名。没有这个文件也能正常构建，只是要能连上 CDN。
CDN_HOOK="${CDN_HOOK:-$SRC/../hooks/jsdelivr-redirect.cjs}"
if [ -f "$CDN_HOOK" ]; then
  export NODE_OPTIONS="--require $CDN_HOOK --max-old-space-size=${BUILD_MEM:-1400}"
else
  export NODE_OPTIONS="--max-old-space-size=${BUILD_MEM:-1400}"
fi
# pnpm 默认把编译缓存放 /tmp（内存盘），必须显式指到磁盘
export NODE_COMPILE_CACHE="${NODE_COMPILE_CACHE:-/root/.cache/node-compile-cache}"
mkdir -p "$NODE_COMPILE_CACHE"
# 每次构建换一个资源版本号，public/ 下的非哈希脚本自动失效，不用再手工 bump
export PUBLIC_BUILD_ID="$(date +%y%m%d%H%M%S)"

echo "[3/6] 构建中（BUILD_ID=$PUBLIC_BUILD_ID）"
BUILD_START=$(date +%s)
pnpm build
BUILD_SEC=$(( $(date +%s) - BUILD_START ))
echo "      构建耗时 ${BUILD_SEC}s"

# 同步前把当前线上打一份硬链接快照（几乎不占空间，且 rsync 是替换文件而非原地改写，
# 所以旧文件的 inode 内容不会被后续 --delete 影响）
mkdir -p "$DST"
if [ -n "$(ls -A "$DST" 2>/dev/null || true)" ]; then
  rm -rf "$PREV"
  mkdir -p "$PREV"
  cp -al "$DST/." "$PREV/" 2>/dev/null || cp -a "$DST/." "$PREV/"
  echo "[4/6] 已存上一版快照 -> $PREV（$(find "$PREV" -type f | wc -l) 个文件）"
else
  echo "[4/6] 线上为空，跳过快照"
fi

echo "[5/6] 同步产物并重载 Nginx"
rsync -a --delete --exclude '.user.ini' "$SRC/dist/" "$DST/"
nginx -t && systemctl reload nginx

# ---------- 冒烟检查 ----------
FAIL=0
ok()  { printf '  ok    %s\n' "$1"; }
bad() { printf '  FAIL  %s\n' "$1"; FAIL=1; }
chk() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1（期望 $2，实际 $3）"; fi; }

echo "冒烟检查："
# 注意：/moments/ 与 /friends/ 在配置里 enable:false，按设计返回 404，不要列进来
for p in / /about/ /archive/ /albums/ /devices/ /games/ /skills/ /timeline/ /anime/ /compass/ /tags/ /categories/ /series/ /rss.xml /atom.xml /sitemap-index.xml /robots.txt; do
  chk "GET $p" 200 "$(curl -sk -o /dev/null -m 8 -w '%{http_code}' "https://127.0.0.1$p")"
done
chk "错误页返回 404" 404 "$(curl -sk -o /dev/null -m 8 -w '%{http_code}' 'https://127.0.0.1/__smoke_not_found__/')"
chk "robots 未封锁 _astro" 0 "$(curl -sk -m 8 https://127.0.0.1/robots.txt | grep -c 'Disallow: /_astro/' || true)"
chk "live.json 可解析" 1 "$(curl -sk -m 8 https://127.0.0.1/live.json | python3 -c 'import sys,json;json.load(sys.stdin);print(1)' 2>/dev/null || echo 0)"
# 分享卡片不能退化成标题（2026-10-23 起 Layout 有默认描述）
chk "og:description 非空且不等于 og:title" "ok" "$(curl -sk -m 8 https://127.0.0.1/ | python3 -c '
import re,sys
h=sys.stdin.read()
d=re.search(r"property=\"og:description\" content=\"([^\"]*)\"",h)
t=re.search(r"property=\"og:title\" content=\"([^\"]*)\"",h)
print("ok" if d and d.group(1).strip() and (not t or d.group(1)!=t.group(1)) else "bad")
' 2>/dev/null || echo bad)"

BID_HIT=$(curl -sk -m 8 https://127.0.0.1/ | grep -o "?v=$PUBLIC_BUILD_ID" | wc -l | tr -d ' ')
if [ "$BID_HIT" -ge 1 ]; then ok "BUILD_ID 已注入（$BID_HIT 处）"; else bad "BUILD_ID 未注入首页，脚本仍是旧版本号"; fi

FILES=$(find "$DST" -type f | wc -l)

echo "[6/6] 完成：$(date '+%F %T')  文件数 $FILES  构建 ${BUILD_SEC}s"

if [ "$FAIL" != 0 ]; then
  echo "★ 冒烟检查有失败项"
  { echo "====== 部署失败 $(date '+%F %T') ======"
    echo "构建 ${BUILD_SEC}s，冒烟检查有失败项。"
    echo "详见 /root/deploy.log 与 /root/.deploy_state"
  } > "$FAILMARK"
  if [ -d "$PREV" ]; then
    echo "→ 自动回滚到上一版…"
    rsync -a --delete --exclude '.user.ini' "$PREV/" "$DST/"
    systemctl reload nginx
    echo "   回滚完成，线上已恢复为上一版。"
    echo "FAIL $(date '+%F %T') 冒烟检查失败，已自动回滚（构建 ${BUILD_SEC}s）" > "$STATE"
  else
    echo "   没有可用快照，未回滚，需人工处理！"
    echo "FAIL $(date '+%F %T') 冒烟检查失败，无快照未回滚（构建 ${BUILD_SEC}s）" > "$STATE"
  fi
  exit 1
fi

echo "OK $(date '+%F %T') 构建 ${BUILD_SEC}s，文件 $FILES" > "$STATE"
rm -f "$FAILMARK"
exit 0
