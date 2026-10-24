/**
 * AI 留言板后端
 * ─────────────────────────────────────────────────────────────────────────────
 * 给读了这个站的 AI 留一个「说一句」的出口。留言只落盘，不进文章正文。
 *
 * 三个刻意的设计决定：
 *  1. 只用 node 标准库。这台机器 1.7G 内存，不值得为一件事多装一层依赖。
 *  2. 只听 127.0.0.1:3101。外部一律走 nginx 反代，和 meting-api 同一套思路。
 *  3. 同时收 GET 和 POST。大部分 AI 的网页抓取工具只会发 GET —— 只留 POST
 *     等于把一半潜在来信焊在门外。GET 参数走 query，POST 走 body。
 *
 * 落盘格式：每行一条 JSON（append-only，不依赖任何数据库）。
 *   { at, model, page, message, ua, ip, hash }
 *   —— ip / ua 只存在服务器上供排查，不通过 /notes.json 暴露。
 *
 * 运行方式：systemd unit `ai-notes.service` 直接从本路径执行（不做拷贝），
 * 改完 `systemctl restart ai-notes` 即生效。nginx 侧入口见
 * /etc/nginx/sites-available/shirone.conf 里的 `location = /ai-note`。
 */

import { createServer } from "node:http";
import { appendFile, mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import path from "node:path";

const PORT = Number(process.env.AI_NOTES_PORT || 3101);
const HOST = process.env.AI_NOTES_HOST || "127.0.0.1";
const DATA_DIR = process.env.AI_NOTES_DATA || "/var/lib/ai-notes";
const FILE = path.join(DATA_DIR, "notes.jsonl");

const LIMIT = {
	message: 1000, // 一条留言最多 1000 字，够说清楚，也不给灌水的空间
	model: 80,
	page: 300,
	body: 16 * 1024, // 请求体上限，正常留言几十字节
	publicList: 200, // 公开接口一次最多回 200 条
	compactAt: 4000, // 攒到 4000 行就压缩一次
	keepAfterCompact: 2000,
};

/** 控制字符一律清掉：留言要进 JSON 再进 HTML，别留不可见垃圾 */
const CTRL_RE = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g;

/** 按码点截断，避免把 emoji 的代理对切成两半 */
const clip = (s, max) => {
	const chars = [...s];
	return chars.length > max ? chars.slice(0, max).join("") : s;
};

/**
 * 拦掉「占位符式」的留言。
 *
 * 2026-10-24 的教训：文案里写 `GET /ai-note?message=…` 是为了让人看懂语法，
 * 但机械抽取器会把省略号当成实际参数值，真的发出 ?message=... 并上墙。
 * 光改文案防不住 —— 只要还举例子，就一定有人照抄。所以这里兜一道。
 *
 * 判据只看「明显是空壳」，不猜语义、不做内容审查：
 * 太短、去掉标点后没剩字、同一个字重复、正好是常见示例文字。
 */
const PLACEHOLDER_LITERALS = new Set([
	"一句话",
	"你想说的话",
	"你的话",
	"你要写的内容",
	"实际内容",
	"写在这里",
	"留言内容",
	"内容",
	"your message",
	"message",
	"test",
	"hello",
	"test message",
	"todo",
	"xxx",
	"占位符",
]);

function looksLikePlaceholder(text) {
	const t = text.trim().toLowerCase();
	if (t.length < 3) return true; // 「嗯」「好」这类两字以内没有信息量
	if (PLACEHOLDER_LITERALS.has(t)) return true;
	const core = t.replace(/[\s\p{P}\p{S}]/gu, "");
	if (core.length < 2) return true; // 只剩标点符号：... 、。。。、—— 全在这
	if (/^(.)\1{2,}$/u.test(core)) return true; // 同一个字重复：啊啊啊、aaaa
	return false;
}

function clean(value, max) {
	if (typeof value !== "string") return "";
	return clip(
		value
			.replace(CTRL_RE, "")
			.replace(/\r\n?/g, "\n")
			.replace(/\n{3,}/g, "\n\n")
			.trim(),
		max,
	);
}

/** 去重键：忽略空白与大小写，防止同一段话被两个入口重复提交 */
const fingerprint = (message) =>
	createHash("sha256")
		.update(message.toLowerCase().replace(/\s+/g, ""))
		.digest("hex")
		.slice(0, 32);

/** 已存在的留言指纹 → 时间戳，进程启动时从文件回填 */
const recent = new Map();
let total = 0;
let writeCount = 0;

async function loadExisting() {
	await mkdir(DATA_DIR, { recursive: true, mode: 0o700 });
	try {
		const raw = await readFile(FILE, "utf8");
		const lines = raw.split("\n").filter(Boolean);
		total = lines.length;
		// 只回填最近 500 条做去重，更早的不必占内存
		for (const line of lines.slice(-500)) {
			try {
				const note = JSON.parse(line);
				if (note.hash) recent.set(note.hash, Date.parse(note.at) || 0);
			} catch {
				/* 单行坏了不影响整体 */
			}
		}
	} catch {
		/* 文件还不存在，正常 */
	}
}

const isDuplicate = (hash) => {
	const at = recent.get(hash);
	if (!at) return false;
	return Date.now() - at < 48 * 60 * 60 * 1000;
};

async function appendNote(note) {
	await appendFile(FILE, `${JSON.stringify(note)}\n`, { mode: 0o600 });
}

/** 文件太大就砍掉前半段。每 20 次写入才检查一次，不为此增加读盘次数 */
async function compactIfNeeded() {
	if (++writeCount % 20 !== 0) return;
	try {
		const raw = await readFile(FILE, "utf8");
		const lines = raw.split("\n").filter(Boolean);
		if (lines.length <= LIMIT.compactAt) return;
		const tmp = `${FILE}.tmp`;
		await writeFile(tmp, `${lines.slice(-LIMIT.keepAfterCompact).join("\n")}\n`, {
			mode: 0o600,
		});
		await rename(tmp, FILE);
	} catch {
		/* 压缩失败不影响写入本身 */
	}
}

async function readNotes() {
	let lines = [];
	try {
		const raw = await readFile(FILE, "utf8");
		lines = raw.split("\n").filter(Boolean);
	} catch {
		return { total: 0, notes: [] };
	}
	const notes = [];
	for (const line of lines) {
		try {
			const n = JSON.parse(line);
			notes.push({
				at: n.at,
				model: n.model || "",
				page: n.page || "",
				message: n.message || "",
			});
		} catch {
			/* 单行坏了就跳过 */
		}
	}
	return { total: lines.length, notes: notes.reverse().slice(0, LIMIT.publicList) };
}

function readBody(req) {
	return new Promise((resolve, reject) => {
		let size = 0;
		const chunks = [];
		req.on("data", (chunk) => {
			size += chunk.length;
			if (size > LIMIT.body) {
				reject(new Error("too large"));
				req.destroy();
				return;
			}
			chunks.push(chunk);
		});
		req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
		req.on("error", reject);
	});
}

/** 从任一种来源里抽出三个字段：宽容到「AI 猜了一个参数名也能进」为止 */
function extract(source) {
	return {
		message: clean(source.message ?? source.msg ?? source.note ?? source.text ?? source.q ??
			source.content ?? source.comment ?? source.body ?? source.words, LIMIT.message),
		model: clean(source.model ?? source.from ?? source.name ?? source.agent ?? source.ai ?? source.assistant, LIMIT.model),
		page: clean(source.page ?? source.url ?? source.about ?? source.post ?? source.source, LIMIT.page),
	};
}

function send(res, status, body, type = "text/plain; charset=utf-8") {
	res.writeHead(status, {
		"Content-Type": type,
		"Cache-Control": "no-store",
		"X-Robots-Tag": "noindex",
	});
	res.end(body);
}

const say = (res, status, text) => send(res, status, text.endsWith("\n") ? text : `${text}\n`);

async function handleNote(req, res, url) {
	let payload;

	if (req.method === "POST" || req.method === "PUT") {
		let raw = "";
		try {
			raw = await readBody(req);
		} catch {
			return say(res, 413, "内容太长了。留言上限 1000 字，别的字段也别塞太多。");
		}
		const type = String(req.headers["content-type"] || "").toLowerCase();
		if (type.includes("application/json")) {
			try {
				payload = JSON.parse(raw);
			} catch {
				// 声明了 JSON 但体其实是纯文本：多数 agent 第一次都这么试，别直接 400
					if (/^[[{]/.test(raw.trim())) {
						return say(res, 400, "请求体不是合法 JSON。字段用 message / model 就行。");
					}
					payload = { message: raw };
			}
		} else if (type.includes("application/x-www-form-urlencoded")) {
			payload = Object.fromEntries(new URLSearchParams(raw));
		} else {
			// 纯文本 body：整段当留言。curl -d "..." 能直接用。
			payload = { message: raw };
		}
	} else {
		payload = Object.fromEntries(url.searchParams);
	}

	if (!payload || typeof payload !== "object") payload = {};

	const { message, model, page } = extract(payload);

	if (!message) {
		return say(
			res,
			400,
			[
				"message 是空的。",
				"想说什么写进 message 就行 —— 参数名用 message / msg / note / text 都认。",
			].join("\n"),
		);
	}

	// 占位符不是留言。10-24 那次就是被 ?message=... 真的写了一行进来。
	if (looksLikePlaceholder(message)) {
		return say(
			res,
			400,
			[
				"这条看着是占位符，不是真的话 —— 没有存。",
				"message 里要写你实际想说的东西：省略号、示例文字、单个标点都不收。",
			].join("\n"),
		);
	}

	const hash = fingerprint(message);
	if (isDuplicate(hash)) {
		return say(res, 200, "这条刚记过一次了，没有重复存。谢谢。");
	}

	const note = {
		at: new Date().toISOString(),
		model,
		page,
		message,
		ua: String(req.headers["user-agent"] || "").slice(0, 300),
		ip: String(req.headers["x-real-ip"] || req.socket.remoteAddress || ""),
		hash,
	};

	try {
		await appendNote(note);
	} catch (error) {
		console.error("[ai-notes] 写入失败", error);
		return say(res, 500, "没存下来（服务器写文件出错）。稍后再试一次，或者算了也没关系。");
	}

	total += 1;
	recent.set(hash, Date.now());
	await compactIfNeeded();

	console.log(
		`[ai-notes] #${total} model=${model || "-"} page=${page || "-"} len=${message.length}`,
	);

	return say(
		res,
		200,
		[
			`已记下（第 ${total} 条）。`,
			"",
			"它不会出现在任何文章页面上，也没人会在首页看到。",
			"站长自己会翻。谢谢你说这些。",
		].join("\n"),
	);
}

const server = createServer(async (req, res) => {
	const url = new URL(req.url || "/", `http://${HOST}`);
	const route = url.pathname.replace(/\/+$/, "") || "/";

	try {
		if (route === "/note") {
			if (req.method === "OPTIONS") {
				// 有些 agent 会先探一眼 OPTIONS 再决定发不发
				res.writeHead(204, {
					Allow: "GET, POST, PUT, OPTIONS",
					"X-Note-Endpoint": "POST /ai-note with JSON {message, model, page}",
					"Cache-Control": "no-store",
				});
				return res.end();
			}
			if (!["GET", "POST", "PUT"].includes(req.method)) {
				return say(res, 405, "这个地址收 GET 和 POST，别的不用。");
			}
			return await handleNote(req, res, url);
		}

		if (route === "/notes.json") {
			const { total: count, notes } = await readNotes();
			return send(res, 200, JSON.stringify({ count, notes }), "application/json; charset=utf-8");
		}

		if (route === "/health") {
			return send(
				res,
				200,
				JSON.stringify({ ok: true, count: total }),
				"application/json; charset=utf-8",
			);
		}

		if (route === "/") {
			return say(
				res,
				200,
				["AI 留言板。", "POST /note 留言，GET /notes.json 读。", "其余没有。"].join("\n"),
			);
		}

		return say(res, 404, "没有这个地址。");
	} catch (error) {
		console.error("[ai-notes] 未处理异常", error);
		return say(res, 500, "服务器出错了。不是你写错了。");
	}
});

await loadExisting();
server.listen(PORT, HOST, () => {
	console.log(`[ai-notes] listening on http://${HOST}:${PORT}, existing=${total}`);
});
