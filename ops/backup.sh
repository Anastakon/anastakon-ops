#!/usr/bin/env bash
# Shirone 源码每日快照（git bundle）
#
# 为什么是 bundle 而不是 tar：bundle 是单个文件、自带完整历史、可直接 clone 回来，
# 体积比 tar 整个工作区小得多，且能验证完整性（git bundle verify）。
#
# 这解决的是「误操作 / 仓库损坏 / 单块盘」的风险。
# 真正的地域级异地备份仍需把 latest.bundle 定期拷到本机或私有仓库。
set -euo pipefail

SRC="${SITE_SRC:-/root/Shirone}"
DIR="${BACKUP_DIR:-/root/backups/git}"
KEEP="${BACKUP_KEEP:-7}"

mkdir -p "$DIR"
cd "$SRC"

# 有未提交改动时先提醒（bundle 只含已提交内容）
DIRTY=$(git status --porcelain | wc -l | tr -d ' ')
if [ "$DIRTY" != "0" ]; then
  echo "WARN: 工作区有 $DIRTY 个未提交改动，这部分不会进快照"
fi

STAMP=$(date +%Y%m%d-%H%M%S)
OUT="$DIR/shirone-$STAMP.bundle"

git bundle create "$OUT" --all --quiet
git bundle verify "$OUT" >/dev/null 2>&1 || { echo "FAIL: bundle 校验失败"; rm -f "$OUT"; exit 1; }

ln -sfn "$OUT" "$DIR/latest.bundle"

# 系统侧配置也一起快照：光有源码还不能重建这台机器（nginx 规则、定时任务、日志策略
# 都在 /etc，重装一次全丢）。下面这份清单按你自己的机器改 —— 用不到的路径直接删，
# --ignore-failed-read 会跳过不存在的文件。
ETC="$DIR/etc-$STAMP.tar.gz"
(
  cd /
  tar czf "$ETC" --ignore-failed-read \
    etc/nginx/nginx.conf \
    etc/nginx/sites-available/shirone.conf \
    etc/nginx/sites-available/openlist-pan.conf \
    etc/logrotate.d/nginx \
    etc/systemd/system/meting-*.service \
    etc/systemd/system/meting-*.timer \
    etc/systemd/system/ai-notes.service \
    etc/systemd/system/shirone-*.service \
    etc/systemd/system/shirone-*.timer \
    etc/update-motd.d/99-shirone-deploy \
    root/ai-notes/data \
    2>/dev/null || true
)
if [ -s "$ETC" ]; then
  ln -sfn "$ETC" "$DIR/latest-etc.tar.gz"
else
  echo "WARN: 系统配置打包为空，跳过"
  rm -f "$ETC"
fi

# 只保留最近 KEEP 份
ls -1t "$DIR"/shirone-*.bundle 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
ls -1t "$DIR"/etc-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f

SIZE=$(du -h "$OUT" | cut -f1)
COUNT=$(ls -1 "$DIR"/shirone-*.bundle 2>/dev/null | wc -l | tr -d ' ')
echo "$(date '+%F %T') OK $OUT ($SIZE) 共保留 $COUNT 份，脏文件 $DIRTY"
