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
            detail = f"{passed}/{total} 通过，但脚本退出码 {code}（半程中断）"
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


def exp_invitation(stats):
    """旧窗口：判据一字未改。改判据等于把过去说的话作废，不能干。"""
    return {
        "id": "invitation-window",
        "question": "一个只做 GET 的客户端，会不会在一个只提供机器可读契约的站上完成一次写？",
        "t0": iso(EXP_INV_T0),
        "t0_meaning": "/openapi.yaml 与 /api/v1 首次返回 200 的时刻（nginx 访问日志实测）。"
                      "此前的流量不计入窗口。",
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


def experiments(stats):
    return [exp_invitation(stats), exp_return_trip(stats)]

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
    exps = experiments(stats)

    ok = sum(1 for i in items if i["ok"] is True)
    bad = sum(1 for i in items if i["ok"] is False)
    unknown = sum(1 for i in items if i["ok"] is None)

    doc = {
        "schema": 2,
        "schema_note": "schema 1 只有一个 experiment 字段；schema 2 换成 experiments 数组，"
                       "因为现在有两个窗口并列，字段名该是复数。旧窗口的判据一个字都没改。",
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
            lines.append(f"#   现状：站外来信 {exp['progress']['letters_from_others']} 封")
        lines.append("")
    tmp = os.path.join(OUT_DIR, ".status.txt.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    os.replace(tmp, os.path.join(OUT_DIR, "status.txt"))

    print(f"wrote {OUT_DIR}/status.json  ok={ok} failed={bad} unknown={unknown}")
    sys.exit(0 if bad == 0 else 0)   # 检查失败不阻止发布，失败本身就是要被看见的内容
