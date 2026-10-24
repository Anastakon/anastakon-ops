/**
 * 构建期 CDN 重定向钩子（服务器 /root/hooks/jsdelivr-redirect.cjs）
 *
 * 背景：这台服务器访问 cdn.jsdelivr.net 被 SNI 阻断（握手即被 RST），
 * 而 fastly.jsdelivr.net / gcore.jsdelivr.net 正常可达，且 *.jsdelivr.net
 * 是同一张通配证书，直接换域名即可。
 *
 * 用法：NODE_OPTIONS="--require /root/hooks/jsdelivr-redirect.cjs ..." pnpm build
 */
const MIRRORS = [
	["cdn.jsdelivr.net", "fastly.jsdelivr.net"],
];

const realFetch = globalThis.fetch;

function rewrite(url) {
	if (typeof url !== "string") return url;
	let out = url;
	for (const [from, to] of MIRRORS) {
		if (out.includes(from)) out = out.split(from).join(to);
	}
	return out;
}

globalThis.fetch = function (input, init) {
	try {
		if (typeof input === "string") {
			input = rewrite(input);
		} else if (input && typeof input.url === "string") {
			const next = rewrite(input.url);
			if (next !== input.url) {
				input = new Request(next, input);
			}
		} else if (input instanceof URL) {
			const next = rewrite(input.href);
			if (next !== input.href) input = new URL(next);
		}
	} catch {
		// 改写失败就用原始请求，不阻断构建
	}
	return realFetch.call(this, input, init);
};
