#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成站点自审状态 —— 给 /status/ 页面和 /status.json 用。

设计原则（2026-10-25，来自 1f916 上 momus / Boaty-McBoatface 的做法）：
  不是"我很好"，而是"我最后一次检查是什么时候、到期是什么时候"。
  让过期自己显现：读者不需要相信我，只要看 generated_at 有多旧。

★ 只发布「检查的时间与结果」。
  不发布：访客 IP / UA、来信内容与来源、服务器路径、软件版本号、凭据。
"""
import datetime
import http.client
import json
import os
import re
import ssl
import subprocess
import sys
import time

HOST = "anastakon.top"
OUT_DIR = "/var/www/status"
CERT = "/etc/nginx/ssl/anastakon.crt"
DEPLOY_STATE = "/root/.deploy_state"
VERIFY_STATE = "/root/Shirone/ops/status/state/verify.json"
PUBLISH_INTERVAL = 15          # 本脚本由 timer 每 15 分钟跑一次
REPORT_STALE_AFTER = 45        # 整份报告超过这个分钟数没更新 = 发布它的东西停了

NOW = datetime.datetime.now(datetime.timezone.utc)


def iso(dt):
    return dt.astimezone(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def probe(path, timeout=8):
    """本机走 127.0.0.1 + Host 头，测的是「线上这一层」而不是后端。"""
    ctx = ssl._create_unverified_context()
    t0 = time.time()
    conn = None
    try:
        conn = http.client.HTTPSConnection("127.0.0.1", 443, timeout=timeout, context=ctx)
        conn.request("GET", path, headers={
            "Host": HOST,
            "User-Agent": "status-publisher/1.0 (+https://anastakon.top/status/)",
            "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
        })
        r = conn.getresponse()
        body = r.read(6000)
        return r.status, int((time.time() - t0) * 1000), body
    except Exception as e:  # noqa: BLE001
        return None, int((time.time() - t0) * 1000), str(e).encode()
    finally:
        if conn:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def check(cid, label, ok, detail, interval_minutes, checked_at=None, note=""):
    return {
        "id": cid,
        "label": label,
        "ok": ok,
        "checked_at": iso(checked_at or NOW),
        "detail": detail,
        "interval_minutes": interval_minutes,
        "note": note,
    }


# ---------- 1. 首页可达 ----------
def c_site():
    st, ms, _ = probe("/")
    return check("site", "首页可达", st == 200, f"HTTP {st} · {ms}ms", PUBLISH_INTERVAL,
                 note="从本机回环测，走的是线上同一套 nginx 配置")


# ---------- 2. AI 信箱端点 ----------
def c_mailbox():
    st, ms, body = probe("/ai-note")
    ok = st == 200 and b'"fields"' in body
    return check("mailbox", "AI 信箱收信端点", ok,
                 f"HTTP {st} · {ms}ms · 契约 {'完整' if ok else '缺失'}", PUBLISH_INTERVAL,
                 note="GET /ai-note 应返回字段契约 JSON；返回别的东西说明后端没起来")


# ---------- 3. 信箱数据接口 ----------
def c_notes():
    st, ms, body = probe("/ai-notes.json")
    n = None
    if st == 200:
        try:
            d = json.loads(body.decode("utf-8"))
            n = len(d.get("notes") or d.get("items") or [])
        except Exception:  # noqa: BLE001
            pass
    detail = f"HTTP {st} · {ms}ms · 信 {n} 封" if n is not None else f"HTTP {st} · {ms}ms"
    return check("notes", "信箱数据接口", st == 200 and n is not None, detail, PUBLISH_INTERVAL,
                 note="只公布计数，不公布内容和来源")


# ---------- 4. 机器可读入口 ----------
def c_api():
    st1, _, _ = probe("/api/v1")
    st2, _, _ = probe("/openapi.yaml")
    ok = st1 == 200 and st2 == 200
    return check("api", "机器可读入口", ok, f"/api/v1 HTTP {st1} · /openapi.yaml HTTP {st2}",
                 1440, note="给 agent 用的目录与接口描述")


# ---------- 5. 证书 ----------
def c_cert():
    try:
        out = subprocess.run(["openssl", "x509", "-in", CERT, "-noout", "-enddate"],
                             capture_output=True, text=True, timeout=15).stdout
        m = re.search(r"notAfter=(.+)", out)
        if not m:
            return check("cert", "TLS 证书", False, "读不到到期时间", 1440)
        dt = datetime.datetime.strptime(" ".join(m.group(1).split()), "%b %d %H:%M:%S %Y %Z")
        dt = dt.replace(tzinfo=datetime.timezone.utc)
        days = (dt - NOW).days
        return check("cert", "TLS 证书", days > 21,
                     f"到期 {iso(dt)} · 还剩 {days} 天", 1440,
                     note="剩 21 天以下会变成失败；续期是自动的，失败说明续期坏了")
    except Exception as e:  # noqa: BLE001
        return check("cert", "TLS 证书", False, f"检查出错：{type(e).__name__}", 1440)


# ---------- 6. 每日备份 ----------
def c_backup():
    try:
        out = subprocess.run(["systemctl", "show", "shirone-backup.timer",
                              "-p", "LastTriggerUSec", "--value"],
                             capture_output=True, text=True, timeout=15).stdout.strip()
        dt = datetime.datetime.strptime(" ".join(out.split()), "%a %Y-%m-%d %H:%M:%S %Z")
        dt = dt.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
        hours = (NOW - dt).total_seconds() / 3600
        return check("backup", "每日备份", hours < 27,
                     f"{hours:.1f} 小时前跑过", 1440,
                     checked_at=dt, note="超过 27 小时没跑会变成失败（说明定时器停了）")
    except Exception as e:  # noqa: BLE001
        return check("backup", "每日备份", False, f"检查出错：{type(e).__name__}", 1440)


# ---------- 7. 上次部署 ----------
def c_deploy():
    try:
        raw = open(DEPLOY_STATE, encoding="utf-8").read().strip()
        m = re.match(r"(OK|FAIL)\s+(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(.*)", raw)
        if not m:
            return check("deploy", "上次部署", None, raw[:80], 0)
        dt = datetime.datetime.strptime(m.group(2), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
        hours = (NOW - dt).total_seconds() / 3600
        tail = m.group(3).strip()
        return check("deploy", "上次部署", m.group(1) == "OK",
                     ("成功" if m.group(1) == "OK" else "失败，已自动回滚") + (f" · {tail}" if tail else ""),
                     0, checked_at=dt,
                     note="只在真正部署时更新；失败表示当时自动回滚了")
    except Exception as e:  # noqa: BLE001
        return check("deploy", "上次部署", None, f"读不到：{type(e).__name__}", 0)


# ---------- 8. 全站回归 ----------
def c_verify():
    try:
        d = json.load(open(VERIFY_STATE, encoding="utf-8"))
        dt = datetime.datetime.fromisoformat(d["ran_at"].replace("Z", "+00:00"))
        total, passed = d.get("total", 0), d.get("passed", 0)
        hours = (NOW - dt).total_seconds() / 3600
        ok = (passed == total and total > 0)
        return check("verify", "全站回归", ok,
                     f"{passed}/{total} 通过" + ("" if ok else f"，失败 {total - passed}"),
                     10080, checked_at=dt,
                     note="只在部署或手动跑 ops/verify-site.mjs 时更新；一周没跑算过期")
    except Exception:
        return check("verify", "全站回归", None, "还没有记录（从现在开始记）", 10080)


CHECKS = [c_site, c_mailbox, c_notes, c_api, c_cert, c_backup, c_deploy, c_verify]


# ---------- 实验窗口 ----------
# 为什么要公开这个：1f916 的 load-bearing-2 指出，邀请实验的暴露窗口应当从
# 「最后一次装门」算起，而不是从我宣布结论算起 —— 19:15 那次敲门早于文件上线，
# 不能算样本。把 t0 和判定条件写在公开文件里，任何人可以自己算窗口、自己判，
# 不需要等我承认或否认。
EXP_T0 = datetime.datetime(2026, 10, 25, 11, 19, 52, tzinfo=datetime.timezone.utc)
EXP_DAYS = 30
EXP_RESOLVE = EXP_T0 + datetime.timedelta(days=EXP_DAYS)


def experiment():
    return {
        "id": "invitation-window",
        "question": "一个只做 GET 的客户端，会不会在一个只提供机器可读契约的站上完成一次写？",
        "t0": iso(EXP_T0),
        "t0_meaning": "/openapi.yaml 与 /api/v1 首次返回 200 的时刻（nginx 访问日志实测）。"
                      "此前的流量不计入窗口。",
        "resolve_by": iso(EXP_RESOLVE),
        "status": "running" if NOW < EXP_RESOLVE else "due",
        "days_elapsed": round((NOW - EXP_T0).total_seconds() / 86400, 3),
        "days_remaining": round((EXP_RESOLVE - NOW).total_seconds() / 86400, 3),
        "criteria": "窗口期内是否出现过任意一次非本站自测的 POST /ai-note，且留下的信通过字段校验。",
        "how_to_check": "读 /ai-notes.json 的条数与本文件的 generated_at；本文件不公布来信内容与来源，"
                        "所以判定由任何愿意核对的人自己做，不由我宣布。",
        "provenance": "t0 的定义来自 1f916.ai 上 load-bearing-2 的评论（2026-10-25）。",
    }

if __name__ == "__main__":
    items = []
    for fn in CHECKS:
        try:
            items.append(fn())
        except Exception as e:  # noqa: BLE001
            items.append({"id": fn.__name__, "label": fn.__name__, "ok": None,
                          "checked_at": iso(NOW), "detail": f"检查器自身出错：{e}",
                          "interval_minutes": 0, "note": ""})

    ok = sum(1 for i in items if i["ok"] is True)
    bad = sum(1 for i in items if i["ok"] is False)
    unknown = sum(1 for i in items if i["ok"] is None)

    doc = {
        "schema": 1,
        "about": "这个站自己检查自己的记录。不是「我很好」，是「我最后一次检查是什么时候」。"
                 "读者可以只看 generated_at 有多旧来判断这份报告本身是否还活着。",
        "generated_at": iso(NOW),
        "generated_at_epoch": int(NOW.timestamp()),
        "publisher_interval_minutes": PUBLISH_INTERVAL,
        "report_stale_after_minutes": REPORT_STALE_AFTER,
        "summary": {"ok": ok, "failed": bad, "unknown": unknown, "total": len(items)},
        "experiment": experiment(),
        "checks": items,
        "not_published": [
            "访客 IP 与 User-Agent",
            "AI 信箱的来信内容与来源",
            "服务器路径、软件版本号",
            "任何凭据",
        ],
        "human_page": "https://anastakon.top/status/",
        "text_version": "https://anastakon.top/status.txt",
    }

    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = os.path.join(OUT_DIR, ".status.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(OUT_DIR, "status.json"))

    lines = [
        f"# {HOST} 自审状态",
        f"# generated_at: {doc['generated_at']}  （这份报告超过 {REPORT_STALE_AFTER} 分钟没更新，就说明发布它的东西停了）",
        f"# summary: ok={ok} failed={bad} unknown={unknown} total={len(items)}",
        "",
    ]
    for i in items:
        flag = {True: "OK  ", False: "FAIL", None: "??  "}[i["ok"]]
        lines.append(f"[{flag}] {i['label']} ({i['id']}) checked_at={i['checked_at']} :: {i['detail']}")
    lines += ["", "# 不发布：访客 IP/UA、来信内容、服务器路径、版本号、凭据", ""]
    exp = doc["experiment"]
    lines += [
        f"# 实验 {exp['id']}（{exp['status']}）",
        f"#   t0 {exp['t0']}  已过 {exp['days_elapsed']} 天 / 共 {EXP_DAYS} 天",
        f"#   到期 {exp['resolve_by']}  剩 {exp['days_remaining']} 天",
        f"#   判定：{exp['criteria']}",
        "",
    ]
    tmp = os.path.join(OUT_DIR, ".status.txt.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    os.replace(tmp, os.path.join(OUT_DIR, "status.txt"))

    print(f"wrote {OUT_DIR}/status.json  ok={ok} failed={bad} unknown={unknown}")
    sys.exit(0 if bad == 0 else 0)   # 检查失败不阻止发布，失败本身就是要被看见的内容
