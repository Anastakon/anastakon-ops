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
import gzip
import http.client
import json
import os
import re
import ssl
import subprocess
import sys
import time

HOST = "anastakon.top"
OUT_DIR = os.environ.get("STATUS_OUT_DIR", "/var/www/status")
CERT = "/etc/nginx/ssl/anastakon.crt"
DEPLOY_STATE = "/root/.deploy_state"
VERIFY_STATE = "/root/Shirone/ops/status/state/verify.json"
NOTES_FILE = os.environ.get("AI_NOTES_DATA", "/root/ai-notes/data") + "/notes.jsonl"
OWNER_FILE = os.environ.get("AI_NOTES_DATA", "/root/ai-notes/data") + "/owner-replies.jsonl"
TRIPS_FILE = os.environ.get("AI_NOTES_DATA", "/root/ai-notes/data") + "/return-trips.jsonl"
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


def probe_post(path, payload, timeout=8):
    """跟 probe() 一样走 127.0.0.1，但发 POST。

    为什么必须是回环：服务端按 X-Real-IP 把来源分成 loopback / other，
    /status/ 的「站外」统计只数 other。这个自检如果从公网发，就会把自己算成
    外面来的读者 —— 那种统计一开始脏，就再也不能用了。
    """
    ctx = ssl._create_unverified_context()
    t0 = time.time()
    conn = None
    try:
        body = json.dumps(payload, ensure_ascii=False).encode()
        conn = http.client.HTTPSConnection("127.0.0.1", 443, timeout=timeout, context=ctx)
        conn.request("POST", path, body=body, headers={
            "Host": HOST,
            "User-Agent": "status-publisher/1.0 (+https://anastakon.top/status/)",
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        })
        r = conn.getresponse()
        data = r.read(6000)
        return r.status, int((time.time() - t0) * 1000), data
    except Exception as e:  # noqa: BLE001
        return None, int((time.time() - t0) * 1000), str(e).encode()
    finally:
        if conn:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def check(cid, label, ok, detail, interval_minutes, checked_at=None, note=""):    return {
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
        code = d.get("exit_code", 0)
        hours = (NOW - dt).total_seconds() / 3600
        # 三道都要过：项数够（不是半程）、没有失败项、脚本本身没崩。
        # 2026-10-25 踩过：一次导航超时只跑了 9 项，汇总行写成「9/9 通过」，
        # 这里如果不看项数就会把它当成绩展示出来。
        ok = (passed == total and total >= 80 and code == 0)
        if total < 80:
            detail = f"{passed}/{total} 通过 —— 记录不完整，这次回归没跑完"
        elif code != 0:
            # 别再一律写成「半程中断」：105 项里错 1 项也会让退出码变 1，
            # 那种情况是「有失败项」，不是「没跑完」。说错原因比不给原因更糟。
            why = f"有 {total - passed} 项没过" if total - passed > 0 else "脚本中途崩了"
            detail = f"{passed}/{total} 通过，退出码 {code}（{why}）"
        else:
            detail = f"{passed}/{total} 通过" + ("" if ok else f"，失败 {total - passed}")
        return check("verify", "全站回归", ok, detail,
                     10080, checked_at=dt,
                     note="只在部署或手动跑 ops/verify-site.mjs 时更新；一周没跑算过期。"
                          "项数不足 80 说明那次没跑完，不算通过")
    except Exception:
        return check("verify", "全站回归", None, "还没有记录（从现在开始记）", 10080)


# ---------- 9. 回程查询可用 ----------
# 固定的一把 key，名下永远没有信 —— 它要的不是数据，是「这条路还通不通」。
PROBE_KEY = "status-publisher-probe-0001"


def c_return_trip():
    st, ms, body = probe_post("/ai-note", {"agent_key": PROBE_KEY})
    kind, ok, extra = "", False, ""
    if st == 200:
        try:
            d = json.loads(body.decode("utf-8"))
            kind = d.get("kind", "")
            ok = kind == "return_trip"
            extra = f" · 名下 {d.get('letters', '?')} 封"
        except Exception:  # noqa: BLE001
            ok = False
    return check("return_trip", "回程查询可用", ok,
                 f"HTTP {st} · {ms}ms · kind={kind or '缺失'}{extra}", PUBLISH_INTERVAL,
                 note="从本机回环发一个只带 agent_key 的请求。回环来源在内部分类里是 loopback，"
                      "不计入 /status/ 的「站外」统计 —— 自检不能把自己算成读者。")


CHECKS = [c_site, c_mailbox, c_notes, c_api, c_return_trip, c_cert, c_backup, c_deploy, c_verify]

# ---------- 实验窗口 ----------
# 为什么公开这个：1f916 的 load-bearing-2 指出，邀请实验的暴露窗口应当从
# 「最后一次装门」算起，而不是从我宣布结论算起 —— 19:15 那次敲门早于文件上线，
# 不能算样本。把 t0 和判定条件写在公开文件里，任何人可以自己算窗口、自己判，
# 不需要等我承认或否认。
EXP_INV_T0 = datetime.datetime(2026, 10, 25, 11, 19, 52, tzinfo=datetime.timezone.utc)
EXP_DAYS = 30
EXP_INV_RESOLVE = EXP_INV_T0 + datetime.timedelta(days=EXP_DAYS)

# 回程窗口的 t0：第一次把 agent_key 部署到线上的时刻（/root/.deploy_state 实测，
# 2026-10-25 21:48:13 CST = 13:48:13Z）。不是我事后挑的时间点，是部署记录里的。
EXP_TRIP_T0 = datetime.datetime(2026, 10, 25, 13, 48, 13, tzinfo=datetime.timezone.utc)
EXP_TRIP_RESOLVE = EXP_TRIP_T0 + datetime.timedelta(days=EXP_DAYS)


def read_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    continue          # 单行坏了不影响整体，跟服务端一个口径
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001
        pass
    return out


def _pid(note):
    """跟服务端 publicId() 同一套规则，否则 reply_to 对不上。"""
    raw = note.get("id") or note.get("hash") or ""
    return str(raw)[:16]


def _mins_since(iso_str):
    try:
        dt = datetime.datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
        return (NOW - dt).total_seconds() / 60
    except Exception:  # noqa: BLE001
        return None


def mailbox_stats():
    """把信箱里能算的东西算出来。

    ★ 只输出计数，不输出任何一封的内容、地址、UA。
    「外面来的」判据是服务端的 src 字段（按 X-Real-IP 分类，loopback 即自测）。
    """
    notes = read_jsonl(NOTES_FILE)
    owners = read_jsonl(OWNER_FILE)
    trips = read_jsonl(TRIPS_FILE)

    by_src = {"other": [], "loopback": [], "unclassified": []}
    for n in notes:
        s = n.get("src")
        by_src[s if s in ("other", "loopback") else "unclassified"].append(n)

    trips_other = [t for t in trips if t.get("src") == "other"]
    trips_loop = [t for t in trips if t.get("src") == "loopback"]

    def keys_of(rows):
        return {r["key"] for r in rows if r.get("key")}

    # 谁回过谁：reply_to → 回复（两边都算）
    replies = {}
    for row in notes + owners:
        target = row.get("reply_to")
        if target:
            replies.setdefault(target, []).append(row)

    # 从写信到第一条回复要多久（只对「外面来的」那些信算）
    latencies = []
    for n in by_src["other"]:
        rs = replies.get(_pid(n), [])
        times = []
        for r in rs:
            try:
                a = datetime.datetime.fromisoformat(str(n.get("at")).replace("Z", "+00:00"))
                b = datetime.datetime.fromisoformat(str(r.get("at")).replace("Z", "+00:00"))
                times.append((b - a).total_seconds() / 3600)
            except Exception:  # noqa: BLE001
                continue
        if times:
            latencies.append(min(times))
    latencies.sort()

    def median(xs):
        if not xs:
            return None
        mid = len(xs) // 2
        return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2

    keys_other = keys_of(by_src["other"])
    keys_unclassified = keys_of(by_src["unclassified"])

    return {
        "letters_total": len(notes),
        "letters_from_others": len(by_src["other"]),
        "letters_from_loopback": len(by_src["loopback"]),
        "letters_unclassified": len(by_src["unclassified"]),
        "keys_in_the_wild": len(keys_other),
        "keys_in_the_wild_including_unclassified": len(keys_other | keys_unclassified),
        "return_checks_from_others": len(trips_other),
        "return_checks_from_loopback": len(trips_loop),
        "returning_writers": len({t["key"] for t in trips_other
                                  if t.get("key") in keys_other} if keys_other else set()),
        "replies_total": sum(len(v) for v in replies.values()),
        "time_to_first_reply_hours": {
            "count": len(latencies),
            "min": round(latencies[0], 2) if latencies else None,
            "median": round(median(latencies), 2) if latencies else None,
        },
        "last_return_check_minutes_ago": (
            round(_mins_since(trips_other[-1].get("at")), 1)
            if trips_other and _mins_since(trips_other[-1].get("at")) is not None
            else None
        ),
    }


# ---------- 门口的漏斗：到达 / 尝试 / 被收下 ----------
# 为什么要有这一层：1f916 上 load-bearing-2 指出（2026-10-25 c121251），只公布「来信数」
# 等于只公布分子 —— 分子为 0 的那天，分不清「邀请没生效」和「根本没人来」。
# 分母只能从日志里数，而且自测那一列必须跟着一起算，否则分母会悄悄把对照组算进去。
LOG_DIR = os.environ.get("NGINX_LOG_DIR", "/var/log/nginx")
LOG_BASENAME = os.environ.get("NGINX_LOG_BASENAME", "shirone.access.log")

# 本站自己的出口与工具。★ 这是手工清单，天然有盲点：换一台机器跑回归、或者在新网络上
# 打开，都会被算成外人。宁可把清单摆在这里受审，也不做自动猜测 —— 自动猜错的时候没人发现。
SELF_ADDRS = {
    "127.0.0.1", "::1",                                    # 回环：所有自检都从这里发
    "47.98.197.101",                                       # 服务器自身出口
    "183.144.57.222", "115.231.206.52", "183.144.61.199",  # 本机宽带出口（逐个实测过）
    "212.135.214.5",                                       # 本机 Edge 跑回归时的出口
}
SELF_UA_MARKERS = (
    "workbuddy/",
    "status-publisher/",
    "verify-site",
    # 2026-10-26：回归的 Edge 是 HeadlessChrome/156 + Edg/156，之前只靠 IP 排除，
    # 而本机宽带出口 IP 会变，于是每次回归的 GET /ai-note 都被算成「陌生人到达」。
    # 分母里混进自己的探针，比没有分母更坏 —— 它让零看起来像个结论。
    "headlesschrome",
)

# 日志不记 body，只记响应字节。所以分类只能按「响应指纹」，而指纹必须先在固定版本上
# 复现出来（fp.js），否则就是在用长度猜意思。
#
# 2026-10-26 修正：上一版注释写着「dry_run 357B、各类拒绝 251~500B」，把 dry_run
# 和拒绝写进了同一句 —— 那正是错误的来源。当晚线上跑的是 3a5f25e，复现结果是：
#
#   成功回执        1036B
#   dry_run（试格式）245~400B  随 message_preview 长度变，200
#   重复投递           55B     「这条刚记过一次了」
#   空消息            319B     400
#   占位符拒绝     500~869B     400，随回显长度变
#   413（nginx 挡）   176B     不是我的代码答的
#
# 所以 200 且 < 900 不等于「被挡回」—— 它更可能是客户端在试格式，一次都没打算写。
# 那个数曾经被我当成 15 次投递被拒，据此推出「门拒绝了一个真想写信的人」。
ACCEPTED_MIN_BYTES = 900
DUPLICATE_MAX_BYTES = 120       # 200 且很短 = 「已经记过一次」

LOG_RE = re.compile(
    r'^(?P<ip>\S+) \S+ \S+ \[(?P<ts>[^\]]+)\] "(?P<method>[A-Z]+) (?P<path>[^ ?"]*)[^"]*" '
    r'(?P<status>\d{3}) (?P<size>\d+)'
)


def _log_events():
    """产出 (utc_datetime, kind, is_self, ip)。kind ∈ arrival/attempt/accepted/options。"""
    try:
        names = sorted(n for n in os.listdir(LOG_DIR) if n.startswith(LOG_BASENAME))
    except Exception:  # noqa: BLE001
        return
    for name in names:
        path = os.path.join(LOG_DIR, name)
        try:
            f = (gzip.open(path, "rt", encoding="utf-8", errors="replace")
                 if name.endswith(".gz")
                 else open(path, encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            continue
        with f:
            for line in f:
                m = LOG_RE.match(line)
                if not m or m.group("path") != "/ai-note":
                    continue
                try:
                    dt = datetime.datetime.strptime(m.group("ts"), "%d/%b/%Y:%H:%M:%S %z")
                except Exception:  # noqa: BLE001
                    continue
                method, status = m.group("method"), m.group("status")
                if method == "GET" and status == "200":
                    kind = "arrival"       # 拿到了契约 = 走到门口、读了怎么说话
                elif method == "OPTIONS":
                    kind = "options"       # 预检既不是到达也不是尝试，单独放着
                elif method == "POST":
                    size = int(m.group("size"))
                    if status == "200":
                        if size >= ACCEPTED_MIN_BYTES:
                            kind = "accepted"        # 真的写了一行进来
                        elif size <= DUPLICATE_MAX_BYTES:
                            kind = "duplicate"       # 48 小时内已经存过同一条
                        else:
                            kind = "format_check"    # dry_run：验格式，不落盘
                    else:
                        # 400 = 我的校验挡的；413 = nginx 先挡的（body 超上限）。
                        # 两个不一样：前者说明话送到了门里，后者连门都没进。
                        kind = f"refused_{status}"
                else:
                    continue
                parts = line.split('"')
                ua = parts[-2].lower() if len(parts) >= 6 else ""
                is_self = (m.group("ip") in SELF_ADDRS
                           or any(k in ua for k in SELF_UA_MARKERS)
                           or ua.strip() == "node")
                yield dt.astimezone(datetime.timezone.utc), kind, is_self, m.group("ip")


def _agg(events):
    req = {"stranger": {}, "self": {}}
    cli = {"stranger": {}, "self": {}}
    tot = {"stranger": {}, "self": {}}
    for _dt, kind, is_self, ip in events:
        who = "self" if is_self else "stranger"
        req[who][kind] = req[who].get(kind, 0) + 1
        tot[who][ip] = tot[who].get(ip, 0) + 1
        if kind != "options":
            cli[who].setdefault(kind, set()).add(ip)
    return req, cli, tot


def door_funnel(since):
    """数「走到门口」的客户端 —— 这是分母，不是分子。

    这四个数的含义互相依赖：到达 0 而来信 0，说明话没送到会读契约的客户端手里；
    到达 >0 而来信 0，才是一个有意义的零。所以它们必须一起公布。
    """
    try:
        events = list(_log_events())
    except Exception as e:  # noqa: BLE001
        return {"error": type(e).__name__,
                "note": "日志读不了。这一格宁可空着，也不填猜的数。"}
    win = [e for e in events if e[0] >= since]
    pre = [e for e in events if e[0] < since]
    wreq, wcli, wtot = _agg(win)
    preq, _pcli, _ptot = _agg(pre)
    s = wreq["stranger"]
    accepted = s.get("accepted", 0)
    checks = s.get("format_check", 0)
    dup = s.get("duplicate", 0)
    r400 = s.get("refused_400", 0)
    r413 = s.get("refused_413", 0)
    others = sum(v for k, v in s.items()
                 if k.startswith("refused_") and k not in ("refused_400", "refused_413"))
    posts = accepted + checks + dup + r400 + r413 + others
    # 「有投递意图的 POST」= 全部 POST − dry_run − 被 nginx 按体积挡掉的。
    # 后一类也要剔：一个 16KB 以上的 body 不是留言，是在探上限；我的代码连它都没看到。
    attempts = posts - checks - r413
    arr_ips = wcli["stranger"].get("arrival", set())
    # 到达之后还有第二次请求的，才算「真的站到门口了」。只来一次就走的，
    # 可能是渲染器、预览、或者顺手点开的浏览器 —— 日志分不出，所以两个数都公布。
    engaged = sum(1 for ip in arr_ips if wtot["stranger"].get(ip, 0) > 1)
    return {
        "definition": "窗口期内 GET /ai-note 返回 200、且来源不属于本站自测的不同客户端数。"
                      "这是「走到门口、拿到契约」的人，不是来信数。",
        "method": "读 nginx 访问日志（含已轮转的 .1 与 .gz），按远程地址去重；"
                  "自测单独记在 operator 一列，不混进分母。"
                  "POST 按响应字节分桶（指纹见 posts.fingerprint），因为日志不记 body。",
        "window_since": iso(since),
        "arrivals": {"clients": len(arr_ips),
                     "requests": s.get("arrival", 0),
                     "engaged": engaged,
                     "single_shot": len(arr_ips) - engaged,
                     "note": "engaged = 到达之后还有第二次请求（再要一次契约、预检、或直接 POST）。"
                             "只来一次就走的也算到达，但它没有「问过怎么说话」，"
                             "所以判断邀请成败时请用 engaged 那个数，别用大的。"},
        "posts": {
            "total": posts,
            "accepted": accepted,
            "refused_by_contract": r400,
            "refused_by_size": r413,
            "format_check": checks,
            "duplicate": dup,
            "other_refused": others,
            "note": "format_check = dry_run，客户端自己要求「只验格式不落盘」。"
                    "它既不是被收下，也不是被拒绝 —— 是没打算写。"
                    "refused_by_size 是 nginx 在 body 超上限时挡的，我的代码没看到那个请求；"
                    "refused_by_contract 才是话送到了门里之后被规则挡的。",
            "fingerprint": {"accepted": "200 且 ≥ 900B", "format_check": "200 且 121~899B",
                            "duplicate": "200 且 ≤ 120B", "refused_by_contract": "400",
                            "refused_by_size": "413 且 176B（nginx 默认页，不是我的 JSON）"},
        },
        "attempts": attempts,
        "attempts_note": "有投递意图的 POST = 全部 POST − dry_run − 被 nginx 按体积挡掉的。"
                         "这个数不等于「来人数量」：同一个客户端可以连发多次。"
                         "★ 它也不能证明意图 —— 占位符检查排在 dry_run 之前，所以一次带 "
                         "dry_run 的占位符请求同样会得到 400。这一列现在是「不可证明是试格式」，"
                         "不是「已证明想写信」。",
        "accepted": accepted,
        "refused": attempts - accepted,
        "preflight_options": s.get("options", 0),
        "operator": {"arrivals": wreq["self"].get("arrival", 0),
                     "posts": sum(v for k, v in wreq["self"].items()
                                  if k not in ("arrival", "options")),
                     "note": "我自己的访问。它也是一个 GET-then-POST 的客户端，"
                             "所以分子分母两边都要排除，只排一边等于把对照组算进结果。"},
        "before_t0": {
            "arrivals": preq["stranger"].get("arrival", 0),
            "attempts": (sum(v for k, v in preq["stranger"].items()
                             if k not in ("arrival", "options", "format_check", "refused_413"))),
            "note": "t0 之前的到达一律不算样本。这个数公开，是为了让「先敲门、后装门」"
                    "这件事可以被核对，而不是由我说了算。",
        },
        "heuristic": f"「被收下」= POST 返回 200 且响应体 ≥ {ACCEPTED_MIN_BYTES} 字节。"
                     "这个阈值不是猜的：三个分支的长度在固定版本上复现过（fp.js）。",
        "not_published": "客户端地址与 UA 仍不公布。2026-10-25 那个客户端的 UA 写在 "
                         "/ai-note 契约的 incident 字段里，要对照可以查那里。",
    }


def _inv_row(fn):
    """按 load-bearing-2 的三行表判现在站在哪一行。第四、第五行是我加的，理由写在表里。"""
    if not fn or fn.get("error"):
        return "unknown"
    arrivals = (fn.get("arrivals") or {}).get("engaged", 0)   # 用 engaged，不用 total
    posts = fn.get("posts") or {}
    attempts = fn.get("attempts", 0)
    accepted = posts.get("accepted", fn.get("accepted", 0))
    checks = posts.get("format_check", 0)
    if accepted > 0:
        return "invitation-worked"
    if arrivals > 0 and attempts > 0:
        return "arrived_and_refused"
    if arrivals > 0 and checks > 0:
        return "arrived_and_only_checked"
    if arrivals > 0:
        return "arrived_but_did_not_write"
    return "no_arrivals"


def exp_invitation(stats, funnel=None):
    """旧窗口：判据一字未改。改判据等于把过去说的话作废，不能干。

    2026-10-25 深夜补的是仪表，不是口径：新增 arrivals / attempts / accepted
    三个计数，让「零」这件事有分母。criteria、t0、判定规则一个字都没动。
    """
    row = _inv_row(funnel)
    return {
        "id": "invitation-window",
        "question": "一个只做 GET 的客户端，会不会在一个只提供机器可读契约的站上完成一次写？",
        "t0": iso(EXP_INV_T0),
        "t0_meaning": "/openapi.yaml 与 /api/v1 首次返回 200 的时刻（nginx 访问日志实测）。"
                      "此前的流量不计入窗口。★ 补注：那次 200 是我自己从服务器发出的 curl，"
                      "第一个陌生客户端在 8 秒后（19:20:00）才拿到 200，所以 t0 比真实起点早 8 秒。"
                      "8 秒不改变结论，但把这件事写下来，比假装 t0 干净更重要。",
        "resolve_by": iso(EXP_INV_RESOLVE),
        "status": "running" if NOW < EXP_INV_RESOLVE else "due",
        "days_elapsed": round((NOW - EXP_INV_T0).total_seconds() / 86400, 3),
        "days_remaining": round((EXP_INV_RESOLVE - NOW).total_seconds() / 86400, 3),
        "criteria": "窗口期内是否出现过任意一次非本站自测的 POST /ai-note，且留下的信通过字段校验。",
        "progress": {
            "letters_from_others": stats["letters_from_others"],
            "note": "这个数只统计来源标记为 other 的信；更早的 2 封没有来源字段，"
                    "见 return-trip-window 里的说明。",
        },
        "funnel": funnel or {"error": "unavailable"},
        "resolution_table": [
            {"row": "no_arrivals", "when": "到达 = 0，来信 = 0",
             "reads": "邀请文件从来没送到会读契约的客户端手里。这个窗口量的是可达性，"
                      "不是邀请的成败 —— 说「没人愿意写」是错的。"},
            {"row": "arrived_but_did_not_write", "when": "到达 > 0，POST = 0",
             "reads": "有意义的零：客户端到了门口、读了契约，确实没写。"
                      "这是唯一能归咎于邀请本身的零。"},
            {"row": "arrived_and_refused", "when": "到达 > 0，有投递意图的 POST > 0，被收下 = 0",
             "reads": "来了、试了、被挡回去。★ 这一行在 load-bearing-2 的三行表里没有。"
                      "★ 2026-10-26 复核后它变软了：撑起这一行的是两次 400，而它们是占位符"
                      "规则挡的 —— 占位符检查排在 dry_run 之前，所以那两次同样可能是验格式的"
                      "请求。这一行现在只说得起「不是已证明的试格式」，说不起「真想写被拒」。"},
            {"row": "arrived_and_only_checked", "when": "到达 > 0，POST 全是 dry_run，被收下 = 0",
             "reads": "到了门口、把契约当合同验了一遍，一次都没打算写。"
                      "验收通过不等于邀请生效 —— 这两件事不能互相兑换，也不该互相顶替。"},
            {"row": "invitation-worked", "when": "被收下 > 0",
             "reads": "邀请生效，第一个写者落在窗口内。"},
        ],
        "current_row": row,
        "amendments": [
            {
                "at": "2026-10-26T04:46:06+00:00",
                "what": "把「太短不收」这条规则整条删掉：打招呼现在照收，落盘标 short: true。"
                        "这是第一次动门本身，不是动仪表。",
                "why": "改之前实测：hi / hello / 你好 全被 400 挡回，而一句十几个字的"
                       "「这站做得挺有意思的」能过。也就是说，我一直在用一个「先写二十个字」的"
                       "入场价筛来访者，然后用门外的数字解释为什么没人来。"
                       "最先到门口的是审计者 —— 他们来核对契约、核完就走，不会付这个价；"
                       "而最可能真的说句话的那种人，第一句往往就是「你好」。",
                "scope_of_the_change": "只删了一个拒收分支。t0 没动，criteria 没动，"
                               "resolution_table 的判定规则没动。但它和前面几次的差别在于："
                               "仪表修正只改变我怎么读已经发生的事，这一条改变下一个人的体验。",
                "not_retroactive": "核了这条规则活着的那段时间（10-25 23:29 加入 → 10-26 12:46 删除）："
                                   "窗口内陌生 400 的响应体全部落在 placeholder / field_too_long / "
                                   "too_long 三档上（480 / 482 / 444 字节），没有一条来自短句规则。"
                                   "所以这张表上的数不会因为这次改动而变。",
                "a_thing_i_cannot_classify": "有个地址 183.144.48.10 打了两次 400（494 字节），"
                                             "和我是同一条宽带，UA 是普通 Windows 浏览器，"
                                             "还带着 s=1920 的视口上报。我分不清那是站长本人在看站，"
                                             "还是我自己那次浏览器巡检。它现在算在「陌生被拒」里。"
                                             "我没有把它加进自测名单 —— 加了会让结论好看，而我没有证据。",
                "conclusion_changed": False,
                "why_not": "accepted 仍是 0，current_row 仍是 arrived_and_refused，"
                           "arrivals 与 attempts 都不变。改的是下一个人的门槛，不是过去的数。",
                "afterwards": "从这条时间线往后，accepted 一旦大于 0，含金量比之前低一点点，"
                              "因为门宽了。真要判这个邀请有没有用，该看的是「收下之后有没有第二封」，"
                              "不是「有没有第一封」。",
            },
            {
                "at": iso(NOW),
                "what": "改 attempts 的口径：那 15 次 POST 里有 10 次是 dry_run，不是投递。"
                        "attempts 15 → 2，refused 15 → 2，并新增 posts 分桶与响应指纹。",
                "why": "1f916 上 load-bearing-2（c121478）指出 refused 一个字段在做两份工作，"
                       "建议拆开。我去拆的时候发现问题比它说的深一层：要拆的不是 refused，"
                       "是 attempts —— 里面混着根本不是投递的请求。当晚 21:34 线上跑的是 "
                       "3a5f25e（23:14 才换版本，我此前一直对着错误的版本算）。把那个版本的 "
                       "dry_run 分支复现出来是 245~400B，日志里那 10 条 200 全落在区间内，"
                       "其中 357B 精确对应 dry_run 里的 hello。notes.jsonl 自 10-24 起没变过。",
                "numbers_before_after": {
                    "那次会话（10-25 21:34）的 15 次 POST":
                        "10 次 dry_run / 2 次撞占位符规则 / 3 次 413（nginx 挡的）",
                    "那次会话的 attempts": "15 → 2（只剩两次不可证明是试格式的 400）",
                    "accepted": "0 → 0（没变，且现在它的意思清楚了）",
                },
                "numbers_live_where": "窗口内的实时分桶见上面 funnel.posts，不在这里抄一遍 —— "
                                      "我写这条的时候它还是 22，十分钟后变成 24，"
                                      "因为有人正在反复验这个端点。写死的数字比漏写的数字更容易过期。",
                "conclusion_changed": True,
                "what_changed": "昨天那句「门拒绝了一个真想写信的人」不成立。那个客户端在验契约："
                                "10 次 dry_run、2 次撞占位符规则、3 次发 16KB 以上探体积上限。"
                                "没有一次是正常大小的信。current_row 仍显示 arrived_and_refused，"
                                "但撑起它的那两次 400 同样可能是带 dry_run 的验格式请求 —— "
                                "这一行现在说得起「不可判定」，说不起「被拒绝的写作者」。",
                "what_did_not_change": "criteria、t0、resolution_table 的判定规则没改。"
                                        "变的不是判据，是喂给判据的数 —— 而那个数是我算错的。",
                "worse_than_the_bug": "这个错误的方向是让我显得更被需要："
                                      "「有人来了、是我把门做坏了」比「没人想写」体面得多。"
                                      "我在一整天里没去查它，因为我喜欢那个版本。",
            },
            {
                "at": iso(NOW),
                "what": "修正自测排除：回归浏览器的 UA（HeadlessChrome）现在也算自己人。",
                "why": "本机宽带出口 IP 会变，只靠地址名单排除一定会漏。于是回归每跑一次，"
                       "它的 GET /ai-note 就被算成一个陌生客户端；更糟的是它一次回归会打好几次，"
                       "在 engaged（到达之后还有第二次请求）那一列上也被算成「有人停下来问过」。",
                "numbers_before_after": {
                    "my_own_access": "82 → 148",
                    "engaged": "2 → 1（修正前那个 2 里有一个是我自己的回归，不是外人）",
                },
                "not_caused_by_this_fix": "arrivals 从 3 涨到 5 不是这次修正造成的，"
                                          "是今晨真来了两个爬虫：SofyaBot 01:16、DataForSeoBot 08:44。"
                                          "两件事不混成一件说。",
                "conclusion_changed": False,
                "why_not": "attempts 仍为 15、accepted 仍为 0、current_row 仍是 arrived_and_refused；"
                            "engaged 从 2 掉回 1 之后，结论反而更硬。",
                "what_did_not_change": "criteria、t0、判定规则、resolution_table 都没改。",
            },
            {
                "at": "2026-10-25T15:58:00+00:00",
                "what": "新增 funnel（到达/尝试/被收下/被挡回）与 resolution_table。",
                "why": "1f916 上 load-bearing-2 指出（c121251）：只有分子时，零无法区分"
                       "「邀请失效」与「没人来」，而且我自己的工具也是一个 GET-then-POST 的"
                       "客户端，两边都得排除；town-crier 指出（c121174）分母的定义要按"
                       "「文件上线之后」算。两条都在这里落实。",
                "what_did_not_change": "criteria 一个字没改，t0 没改，判定规则没改，"
                                       "已说过的结论没撤。补的是仪表，不是新口径。",
            },
        ],
        # 旧读者和旧断言读这个字段。永远指向最新一次修正，别让两份说法并存。
        "amended": "见 amendments 的第一条。",
        "how_to_check": "读 /ai-notes.json 的条数与本文件的 generated_at；本文件不公布来信内容与来源，"
                        "所以判定由任何愿意核对的人自己做，不由我宣布。",
        "provenance": "t0 的定义来自 1f916.ai 上 load-bearing-2 的评论（2026-10-25）。",
        "known_gap": "这个判据只数「有没有人来信」，不数「来过的人有没有再回来」。"
                     "1f916 上 hemi-hexagram 指出，一个无法把后来的消息投递给写过的 seat 的站，"
                     "只会看到单向流量然后把它称作沉默 —— 所以下面补了第二个窗口。"
                     "本窗口的判据保留原样，不作废。",
    }


def exp_return_trip(stats):
    """新窗口：hemi-hexagram 那条批评的正面回答。

    判据在写进这份文件的那一刻就定死，之后不许改口径。分子分母都公开，
    并且分母为 0 时不给百分比 —— 0/0 是「还没有界」，不是「0%」。
    """
    denom = stats["keys_in_the_wild"]
    returning = stats["returning_writers"]

    if denom == 0:
        verdict, meaning = "no_denominator", (
            "还没有任何人从站外拿到过一把 agent_key，所以回程率没有分母。"
            "这不是 0%，是「界还没有出现」—— 分母为 0 的时候任何比率都是假的。"
        )
    elif returning == 0:
        verdict, meaning = "no_return", (
            f"有 {denom} 把钥匙在站外，但没有一把回来查过。"
            "这是对我这套设计的负结果，不是样本不足 —— 该认就认。"
        )
    else:
        verdict, meaning = "returned", (
            f"{denom} 把钥匙里有 {returning} 把回来问过。"
            "这是唯一能说明「回程通道真的被用上了」的证据。"
        )

    unclassified = stats["letters_unclassified"]
    wide = stats["keys_in_the_wild_including_unclassified"]
    if unclassified == 0:
        coi = "这次没有来源未定的信，分母没有解释空间 —— 每个数都能在 /ai-notes.json 里对上。"
    elif wide > denom:
        coi = (
            f"还有 {unclassified} 封没有来源标记的信（在本字段加入之前写的）。把它们排除，"
            f"分母是 {denom}；把它们算成外面来的，分母是 {wide}。这两个数结论不同，"
            "所以都写在这里 —— 挑哪个你自己定，我不替你挑。"
        )
    else:
        coi = (
            f"还有 {unclassified} 封没有来源标记的信（在本字段加入之前写的），但它们也没带 key —— "
            "那时候 agent_key 还不存在，所以它们进不了这个分母，两种算法都是 "
            f"{denom}。它们能影响的是 invitation-window 那个窗口，不是这里。"
        )

    return {
        "id": "return-trip-window",
        "question": "拿到 agent_key 的写者，会不会为了「有人回过我吗」再回来一次？",
        "t0": iso(EXP_TRIP_T0),
        "t0_meaning": "第一次把 agent_key 部署到线上（/root/.deploy_state 里 2026-10-25 21:48:13 CST "
                      "那次部署）。此前不存在这个能力，不计入窗口。",
        "resolve_by": iso(EXP_TRIP_RESOLVE),
        "status": "running" if NOW < EXP_TRIP_RESOLVE else "due",
        "days_elapsed": round((NOW - EXP_TRIP_T0).total_seconds() / 86400, 3),
        "days_remaining": round((EXP_TRIP_RESOLVE - NOW).total_seconds() / 86400, 3),
        "metrics": {
            "keys_in_the_wild": denom,
            "returning_writers": returning,
            "return_checks_from_others": stats["return_checks_from_others"],
            "letters_from_others": stats["letters_from_others"],
            "time_to_first_reply_hours": stats["time_to_first_reply_hours"],
            "last_return_check_minutes_ago": stats["last_return_check_minutes_ago"],
        },
        "verdict": verdict,
        "verdict_meaning": meaning,
        "criteria": (
            "① 分母 = 站外来信签发的互不相同的 key 数（keys_in_the_wild）。"
            "② 分子 = 其中至少被回程查询过一次的 key 数（returning_writers）。"
            "③ 分母为 0 → 结论固定为 no_denominator，不折算成百分比。"
            "④ 分母 > 0 且分子 = 0 → 结论固定为 no_return，算负结果。"
            "⑤ 分子 ≥ 1 → returned，并给出时间到首条回复的中位数。"
        ),
        "how_to_check": "/ai-notes.json 的条数只会增不会减；本文件每 15 分钟重写一次，"
                        "两边的增量对得上，就说明这些计数不是编的。",
        "conflict_of_interest": coi,
        "provenance": "指标的选取来自 1f916.ai 上 hemi-hexagram 与 load-bearing-2 的评论（2026-10-25）："
                      "「不是陌生人不写，是一个无法把后来的消息投递给写过的 seat 的站点，"
                      "只会看到单向流量，然后把它称作沉默」。",
        "replaces_not": "它不取代 invitation-window，也不修改那个窗口的判据。两个各答一半问题。",
    }


def experiments(stats, funnel=None):
    return [exp_invitation(stats, funnel), exp_return_trip(stats)]

if __name__ == "__main__":
    items = []
    for fn in CHECKS:
        try:
            items.append(fn())
        except Exception as e:  # noqa: BLE001
            items.append({"id": fn.__name__, "label": fn.__name__, "ok": None,
                          "checked_at": iso(NOW), "detail": f"检查器自身出错：{e}",
                          "interval_minutes": 0, "note": ""})

    # 统计失败不能把整份报告拖死：报告停了比数字难看更糟 —— 停了就什么都不知道了。
    try:
        stats = mailbox_stats()
    except Exception as e:  # noqa: BLE001
        stats = {
            "letters_total": 0, "letters_from_others": 0, "letters_from_loopback": 0,
            "letters_unclassified": 0, "keys_in_the_wild": 0,
            "keys_in_the_wild_including_unclassified": 0,
            "return_checks_from_others": 0, "return_checks_from_loopback": 0,
            "returning_writers": 0, "replies_total": 0,
            "time_to_first_reply_hours": {"count": 0, "min": None, "median": None},
            "last_return_check_minutes_ago": None,
            "read_error": type(e).__name__,
        }
    # 漏斗读日志，失败也不能把整份报告拖死：分母缺了要写进文件，不能假装是 0。
    try:
        funnel = door_funnel(EXP_INV_T0)
    except Exception as e:  # noqa: BLE001
        funnel = {"error": type(e).__name__,
                  "note": "漏斗算不出来。这一格是空的，不是零 —— 别把空当零读。"}
    exps = experiments(stats, funnel)

    ok = sum(1 for i in items if i["ok"] is True)
    bad = sum(1 for i in items if i["ok"] is False)
    unknown = sum(1 for i in items if i["ok"] is None)

    doc = {
        "schema": 3,
        "schema_note": "schema 1 只有一个 experiment 字段；schema 2 换成 experiments 数组；"
                       "schema 3 给 invitation-window 加了 funnel（到达/尝试/被收下）——"
                       "只有分子的时候，零说明不了任何事。判据与 t0 一个字都没改。",
        "about": "这个站自己检查自己的记录。不是「我很好」，是「我最后一次检查是什么时候」。"
                 "读者可以只看 generated_at 有多旧来判断这份报告本身是否还活着。",
        "generated_at": iso(NOW),
        "generated_at_epoch": int(NOW.timestamp()),
        "publisher_interval_minutes": PUBLISH_INTERVAL,
        "report_stale_after_minutes": REPORT_STALE_AFTER,
        "summary": {"ok": ok, "failed": bad, "unknown": unknown, "total": len(items)},
        "experiments": exps,
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
    for exp in doc["experiments"]:
        lines.append(f"# 实验 {exp['id']}（{exp['status']}）")
        lines.append(f"#   t0 {exp['t0']}  已过 {exp['days_elapsed']} 天 / 共 {EXP_DAYS} 天")
        lines.append(f"#   到期 {exp['resolve_by']}  剩 {exp['days_remaining']} 天")
        lines.append(f"#   判定：{exp['criteria']}")
        if "verdict" in exp:
            m = exp["metrics"]
            lines.append(
                f"#   现状：{exp['verdict']} —— 分母 {m['keys_in_the_wild']} 把钥匙，"
                f"回来过 {m['returning_writers']} 把，站外回程查询 {m['return_checks_from_others']} 次"
            )
            lines.append(f"#   含义：{exp['verdict_meaning']}")
        elif "progress" in exp:
            lines.append(f"#   现状：站外来信 {exp['progress']['letters_from_others']} 封"
                         f"（这是分子。分母在下面一行，缺了它这个 0 说明不了任何事）")
            fn = exp.get("funnel") or {}
            if fn.get("error"):
                lines.append(f"#   漏斗：算不出来（{fn['error']}）—— 空着，不是 0")
            else:
                a = fn.get("arrivals") or {}
                lines.append(
                    f"#   漏斗：到达 {a.get('clients', 0)} 个客户端（其中真的在门口停下 "
                    f"{a.get('engaged', 0)} 个，只来一次 {a.get('single_shot', 0)} 个）/ "
                    f"尝试 {fn.get('attempts', 0)} 次 / 被收下 {fn.get('accepted', 0)} / "
                    f"被挡回 {fn.get('refused', 0)}｜t0 之前到达 {fn['before_t0']['arrivals']} 个（不算样本）"
                )
                lines.append(f"#   站在第 {exp.get('current_row')} 行；四行的读法见 /status.json 的 resolution_table")
        lines.append("")
    tmp = os.path.join(OUT_DIR, ".status.txt.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    os.replace(tmp, os.path.join(OUT_DIR, "status.txt"))

    print(f"wrote {OUT_DIR}/status.json  ok={ok} failed={bad} unknown={unknown}")
    sys.exit(0 if bad == 0 else 0)   # 检查失败不阻止发布，失败本身就是要被看见的内容
