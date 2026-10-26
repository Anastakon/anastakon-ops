// Shirone 站点回归检查（真浏览器跑，不是 curl）
// 用法: node verify-site.mjs [baseUrl]      默认 https://anastakon.top
// 退出码: 0 全过 / 1 有失败项 / 2 脚本自身异常
import puppeteer from 'puppeteer-core';

const BASE = (process.argv[2] || 'https://anastakon.top').replace(/\/$/, '');
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const results = [];
function chk(name, pass, detail = '') {
	results.push({ name, pass: !!pass });
	console.log(`  ${pass ? 'ok  ' : 'FAIL'}  ${name}${detail ? '   (' + detail + ')' : ''}`);
}

const browser = await puppeteer.launch({
	executablePath: EDGE,
	headless: 'new',
	protocolTimeout: 300000,
	args: ['--no-sandbox', '--disable-dev-shm-usage'],
});
const page = await browser.newPage();
await page.setViewport({ width: 1280, height: 900 });
// 关掉浏览器缓存：每次导航都是真实回源，避免 304 把状态码检查弄成假失败
await page.setCacheEnabled(false);

let pageErrors = [];
let badRes = [];
let liveHits = [];
page.on('pageerror', (e) => pageErrors.push(e.message));
page.on('response', (r) => {
	const u = r.url();
	if (r.status() >= 400 && /\.(webp|png|jpe?g|svg|gif|avif|css|js)(\?|$)/i.test(u)) {
		badRes.push(r.status() + ' ' + u.replace(BASE, ''));
	}
});
page.on('request', (r) => {
	if (r.url().includes('/live.json')) liveHits.push(r.url());
});

// ★ 站点对「页面文档」按 IP 限速 1r/s、burst 15（shirone.conf 的 doc_req）。
//   回归是机器连发，不节流会把自己打成 429，从而误报成站点故障。这里排队成 ≥1.1s 一次。
let _lastDocNav = 0;
const slowNavs = [];
/** 记一个「本机浏览器没结算」的页面，同一页只记一次，并留下当时的原因 */
const noteSlowNav = (path, why = '') => {
	if (slowNavs.some((s) => s.path === path)) return;
	slowNavs.push({ path, why });
};
// 这些错误都表示「这台机器的浏览器没结算完」，不是站点故障：
// 超时、上下文被导航销毁、目标被关掉、frame 被替换、网络层报错。
const RECOVERABLE_NAV =
	/timeout|Execution context was destroyed|navigating|Target closed|frame was detached|net::ERR_/i;
async function go(path, wait = 500) {
	const gap = 1100 - (Date.now() - _lastDocNav);
	if (gap > 0) await sleep(gap);
	_lastDocNav = Date.now();
	for (let attempt = 1; attempt <= 2; attempt++) {
		try {
			const resp = await page.goto(BASE + path, {
				waitUntil: attempt === 1 ? 'networkidle2' : 'domcontentloaded',
				timeout: attempt === 1 ? 25000 : 15000,
			});
			await sleep(wait);
			if (attempt > 1) noteSlowNav(path, '第一次没结算，改用 domcontentloaded 拿到 HTTP ' + (resp ? resp.status() : 0));
			return resp ? resp.status() : 0;
		} catch (e) {
			const msg = (e && e.message) ? e.message : String(e);
			// networkidle2 要求「≤2 个连接在飞」。只要有一个资源卡住，它就永远等不到，
			// 而页面本身早就可用了 —— 2026-10-25 实测：本机浏览器对同一批资源（/count、
			// 两张 webp、一张 jpg）全部挂起不结算，同一时刻 curl 取它们都是 0.1s 内 200。
			// 那不是站点故障，是这台机器的网络。
			//
			// 2026-10-25 深夜又踩一次：原本只放行 timeout，于是
			// 'Execution context was destroyed' 把整轮回归打断在第 6 项，还连断两次。
			// 那同样是浏览器没结算（同一时刻 curl /skills/ 是 0.16s 内 200，页面也没有跳转）。
			// 所以改成：可恢复 → 记下来 → 换 domcontentloaded 再试一次 → 仍不行才判 0。
			// 这一项要判的是「页面拿不拿得到 200」，不是「资源有没有全部结算」。断言一条没动。
			if (!RECOVERABLE_NAV.test(msg)) throw e;
			noteSlowNav(path, msg.slice(0, 70));
			if (attempt === 2) return 0;
			await sleep(1200);
		}
	}
	return 0;
}
const evalPage = (fn) => page.evaluate(fn);

async function main() {
	console.log('\n[1] 关键页面可访问');
	// 注意：/moments/ 与 /friends/ 配置里 enable:false，按设计 404，不列入
	const PAGES = [
		'/', '/about/', '/archive/', '/albums/', '/devices/', '/games/', '/skills/',
		'/timeline/', '/anime/', '/compass/', '/tags/', '/categories/',
		'/series/', '/posts/agent-writes-back/', '/rss.xml', '/atom.xml',
		'/sitemap-index.xml', '/robots.txt',
	];
	for (const p of PAGES) {
		const s = await go(p, 300);
		chk('GET ' + p, s === 200, 'HTTP ' + s);
	}

	console.log('\n[2] 首页');
	liveHits = [];
	let st = await go('/', 3000);
	const home = await evalPage(() => ({
		nowLive: (document.getElementById('now-live') || {}).textContent || '',
		canonical: (document.querySelector('link[rel=canonical]') || {}).href || '(无)',
		canonCount: document.querySelectorAll('link[rel=canonical]').length,
		h1: document.querySelectorAll('h1').length,
		hasPlaying: document.body.innerText.includes('正在玩'),
	}));
	chk('首页 HTTP 200', st === 200 || st === 304, 'HTTP ' + st);
	chk('「此刻」机器状态有值', /\d/.test(home.nowLive), home.nowLive.trim().slice(0, 46));
	chk('canonical 指向自身', home.canonical === BASE + '/', home.canonical);
	chk('canonical 唯一', home.canonCount === 1, home.canonCount + ' 个');
	chk('首页有 h1', home.h1 >= 1, home.h1 + ' 个');
	chk('「正在玩」已移除', !home.hasPlaying);
	// live.json 是页面脚本在加载后自行发起的，跳转完成 ≠ 它已经发出。
	// 不等待直接断言，网络一慢就统计到 0 次 —— 站点完全正常，断言却红了。
	await page.waitForResponse((r) => r.url().includes('/live.json'), { timeout: 3000 }).catch(() => {});
	// 新版 puppeteer 移除了 page.waitForTimeout，用原生定时器
	await new Promise((r) => setTimeout(r, 300));
	chk('首页 live.json 只请求 1 次', liveHits.length === 1, liveHits.length + ' 次');

	console.log('\n[3] 关于页');
	liveHits = [];
	st = await go('/about/', 3500);
	const about = await evalPage(() => {
		const c = document.getElementById('lv-card');
		return {
			hasCard: !!c,
			cells: c ? c.querySelectorAll('.lv-c').length : 0,
			foot: c ? ((c.querySelector('.lv-f') || {}).textContent || '') : '',
			canonical: (document.querySelector('link[rel=canonical]') || {}).href || '',
		};
	});
	chk('机器状态卡片存在', about.hasCard);
	chk('卡片为 6 格', about.cells === 6, about.cells + ' 格');
	chk('尾注写明 30 秒', about.foot.includes('30 秒'), about.foot.trim().slice(-26));
	chk('canonical 正确', about.canonical === BASE + '/about/', about.canonical);
	chk('关于页 live.json 只请求 1 次', liveHits.length === 1, liveHits.length + ' 次');

	console.log('\n[4] 文章页');
	liveHits = [];
	st = await go('/posts/agent-website-notes/', 3000);
	const art = await evalPage(() => {
		const root = document.querySelector('main') || document.body;
		const t = root.innerText;
		return {
			h1: document.querySelectorAll('h1').length,
			hasPhrase: t.includes('服务器面板'),
			literalStars: /\*\*服务器面板/.test(t),
			strongPhrase: [...document.querySelectorAll('strong,b')].some((e) => e.textContent.includes('服务器面板')),
			canonical: (document.querySelector('link[rel=canonical]') || {}).href || '',
		};
	});
	chk('文章页 HTTP 200', st === 200 || st === 304, 'HTTP ' + st);
	chk('文章页有 1 个 h1', art.h1 === 1, art.h1 + ' 个');
	chk('canonical 正确', art.canonical === BASE + '/posts/agent-website-notes/', art.canonical);
	chk('Markdown 加粗正常（无字面星号）', art.hasPhrase && !art.literalStars && art.strongPhrase);
	chk('文章页不发 live.json 请求', liveHits.length === 0, liveHits.length + ' 次');
	const aiPostHtml = await (await fetch(BASE + '/posts/agent-writes-back/')).text();
	chk('AI 专文归入「Agents」分类', aiPostHtml.includes('Agents'), '分类缺失');
	const unlockedRes = await fetch(BASE + '/posts/journal-minecraft-essay/');
	const unlockedHtml = await unlockedRes.text();
	chk('Minecraft 文章已解锁', unlockedRes.status === 200 && unlockedHtml.includes('2013年') && !unlockedHtml.includes('输入密码'), 'HTTP ' + unlockedRes.status);

	console.log('\n[5] 公开阅读实验');
	st = await go('/ai-reading/');
	chk('实验页 HTTP 200', st === 200 || st === 304, 'HTTP ' + st);
	const experiment = await evalPage(() => ({
		h1: (document.querySelector('.experiment__title')?.textContent || '').trim(),
		target: [...document.querySelectorAll('a')].some((a) => a.getAttribute('href') === '/posts/agent-website-notes/'),
		question: document.body.innerText.includes('哪一处具体细节'),
		mailbox: [...document.querySelectorAll('a')].some((a) => a.getAttribute('href') === '/ai-note'),
	}));
	chk('实验页标题与统一问题存在', experiment.h1 === '同文同题：AI 阅读实验' && experiment.question, experiment.h1 || '(空)');
	chk('实验页链接目标文章与信箱', experiment.target && experiment.mailbox);
	const sitemapForExperiment = await (await fetch(BASE + '/sitemap-0.xml')).text();
	chk('实验页进入 sitemap', sitemapForExperiment.includes('/ai-reading/'));
	chk('llms 页链接实验', (await (await fetch(BASE + '/llms/')).text()).includes('/ai-reading/'));

	console.log('\n[6] 404 页');
	st = await go('/__verify_not_found_a__', 2600);
	// nf.js 故意延迟 ~900ms 才填充标题（setTimeout(run, 900)），networkidle2 之后立刻读会拿到空值 → 显式轮询
	for (let i = 0; i < 25; i++) {
		const done = await evalPage(() => document.getElementById('nf-title')?.getAttribute('data-done') === '1');
		if (done) break;
		await sleep(200);
	}

	const nf = await evalPage(() => ({
		title: (document.getElementById('nf-title') || {}).textContent || '',
		done: document.getElementById('nf-title')?.getAttribute('data-done') === '1',
		path: (document.getElementById('nf-path') || {}).textContent || '',
		btn: (document.getElementById('nf-random') || {}).textContent || '',
		robots: (document.querySelector('meta[name=robots]') || {}).content || '',
		canonCount: document.querySelectorAll('link[rel=canonical]').length,
		posts: document.querySelectorAll('.nf-post').length,
	}));
	chk('返回 404 状态码', st === 404, 'HTTP ' + st);
	chk('随机标题已填充', nf.done && nf.title.trim().length > 0, nf.title.trim());
	chk('踩空路径已回显', nf.path.includes('__verify_not_found_a__'), nf.path.trim());
	chk('robots 为 noindex', /noindex/.test(nf.robots), nf.robots || '(无)');
	chk('404 页不带 canonical', nf.canonCount === 0, nf.canonCount + ' 个');
	chk('推荐文章已渲染', nf.posts >= 1, nf.posts + ' 篇');
	chk('随机按钮有文案', nf.btn.length > 0, nf.btn.trim());

	const seen = [];
	let dup = 0;
	let emptyBtn = 0;
	for (let i = 0; i < 6; i++) {
		try {
			await page.reload({ waitUntil: 'networkidle2', timeout: 25000 });
		} catch (e) {
			// 同 go()：资源卡住不等于页面没加载，记下来继续，别让整轮作废
			if (!/timeout/i.test(e && e.message ? e.message : '')) throw e;
			noteSlowNav('/ (reload)');
		}
		await sleep(1700);
		const m = await evalPage(() => ({
			t: (document.getElementById('nf-title') || {}).textContent || '',
			b: (document.getElementById('nf-random') || {}).textContent || '',
		}));
		if (seen.length && m.t === seen[seen.length - 1]) dup++;
		if (!m.b) emptyBtn++;
		seen.push(m.t);
	}
	chk('404 文案相邻不重复', dup === 0, dup + ' 次重复');
	chk('404 按钮文案不为空', emptyBtn === 0, emptyBtn + ' 次为空');
	chk('6 次出现多种文案', new Set(seen).size >= 4, new Set(seen).size + ' 种');

	console.log('\n[7] 全局');
	chk('无 JS 报错', pageErrors.length === 0, pageErrors.slice(0, 2).join(' | '));
	chk('无资源加载失败', badRes.length === 0, badRes.slice(0, 3).join(' | '));

	const hr = await fetch(BASE + '/');
	const server = hr.headers.get('server') || '(无)';
	chk('Server 头不暴露版本号', !/\d+\.\d+/.test(server), server);
	chk('安全响应头齐全',
		!!hr.headers.get('x-content-type-options') && !!hr.headers.get('referrer-policy'),
		['x-content-type-options', 'referrer-policy', 'x-frame-options']
			.filter((k) => hr.headers.get(k)).join(','));

	const live = await (await fetch(BASE + '/live.json')).json();
	chk('live.json 可解析且字段完整', live && Object.keys(live).length >= 3, Object.keys(live).join(','));

	console.log('\n[8] 域名与跳转');
	const u = new URL(BASE);
	if (u.protocol !== 'https:') {
		console.log('  skip  BASE 不是 https，跳过域名检查');
	} else {
		const apex = u.hostname.replace(/^www\./, '');

		// 访问 http 时必须 301 到 https（跟随跳转会让状态码变成 200，所以用 manual）
		const httpCase = async (label, url, required) => {
			try {
				const r = await fetch(url, { redirect: 'manual' });
				const loc = r.headers.get('location') || '';
				chk(`http→https 跳转（${label}）`, r.status === 301 && loc.startsWith('https://'),
					r.status + ' → ' + (loc || '(无 Location)'));
			} catch (e) {
				const code = (e.cause && e.cause.code) || e.code || '';
				if (!required && code === 'ENOTFOUND') { console.log(`  skip  ${label} 未解析`); return; }
				chk(`http→https 跳转（${label}）`, false, '请求失败: ' + (code || e.message));
			}
		};
		await httpCase('apex', `http://${apex}/`, true);
		await httpCase('www', `http://www.${apex}/`, false);
		await httpCase('pan', `http://pan.${apex}/`, false);

		// 跳转不能吞掉路径和查询串
		try {
			const r = await fetch(`http://${apex}/posts/anime-watching-guide/?a=1`, { redirect: 'manual' });
			const loc = r.headers.get('location') || '';
			chk('跳转保留路径与查询串', loc.includes('/posts/anime-watching-guide/?a=1'), loc || '(无)');
		} catch (e) {
			chk('跳转保留路径与查询串', false, e.message);
		}

		const h = await fetch(BASE + '/', { redirect: 'manual' });
		const hsts = h.headers.get('strict-transport-security');
		chk('HSTS 已启用', !!hsts, hsts || '(无)');

		const html = await (await fetch(BASE + '/')).text();
		const can = (html.match(/<link[^>]+rel="canonical"[^>]*href="([^"]+)"/) || [])[1] || '';
		chk('canonical 指向正式域名且为 https', can === `https://${apex}/`, can || '(无)');
	}

	// [9] 站点描述与结构化数据：这两项曾被主题占位值坑过，锁在这里防回退
	{
		const rss = await (await fetch(BASE + '/rss.xml')).text();
		const rssDesc = (rss.match(/<description>([^<]*)<\/description>/) || [])[1] || '';
		// 站点描述 2026-10-23 起沿用主题默认值（用户选择保留），这里只锁「不能为空」
		chk('RSS 站点描述非空', !!rssDesc, rssDesc.slice(0, 44) || '(空)');

		const parseLd = (h) =>
			[...h.matchAll(/<script[^>]+application\/ld\+json[^>]*>([\s\S]*?)<\/script>/g)]
				.map((m) => {
					try {
						return JSON.parse(m[1]);
					} catch {
						return null;
					}
				})
				.filter(Boolean);

		const homeHtml = await (await fetch(BASE + '/')).text();
		const homeLd = parseLd(homeHtml);
		chk('首页有 WebSite 结构化数据', homeLd.some((j) => j['@type'] === 'WebSite'), `共 ${homeLd.length} 条 JSON-LD`);

		const aboutHtml = await (await fetch(BASE + '/about/')).text();
		const aboutLd = parseLd(aboutHtml);
		chk('内页有 BreadcrumbList', aboutLd.some((j) => j['@type'] === 'BreadcrumbList'), `共 ${aboutLd.length} 条`);
	}

	console.log('\n[10] 页面抓取限速（只限文档，不限资源）');
	// 目的：允许慢爬，不允许「几秒抓完整站」。用页面内 fetch 连发模拟采集器，不是 curl。
	// 必须真的并发。原来写成 for + await，网络一慢就摊成每秒不到一个请求，
	// 连 1r/s 都打不满，429 永远不出现 —— 断言靠运气过，不是靠限速生效。
	const burst = await page.evaluate(async () => {
		const rs = await Promise.all(
			Array.from({ length: 30 }, (_, i) =>
				fetch('/?rl=' + i, { cache: 'no-store' })
					.then((r) => r.status)
					.catch(() => 0),
			),
		);
		const codes = {};
		for (const c of rs) codes[c] = (codes[c] || 0) + 1;
		return codes;
	});
	chk('连发 30 个页面会出现 429', (burst[429] || 0) > 0, JSON.stringify(burst));
	chk('限速不是全封（仍有正常放行）', (burst[200] || 0) >= 5, `200 x${burst[200] || 0}`);

	// 静态资源必须豁免：真浏览器一个页面就拉几十个请求，被限就是白屏
	const assets = await page.evaluate(async () => {
		const list = [
			...[...document.querySelectorAll('link[rel=stylesheet]')].map((l) => l.href),
			...[...document.querySelectorAll('script[src]')].map((s) => s.src),
		].slice(0, 5);
		const codes = {};
		for (let round = 0; round < 6; round++) {
			for (const u of list) {
				const r = await fetch(u, { cache: 'no-store' });
				codes[r.status] = (codes[r.status] || 0) + 1;
			}
		}
		return codes;
	});
	chk('静态资源连拉不被限速', (assets[429] || 0) === 0, JSON.stringify(assets));

	// 冷却后必须恢复，否则一次误判会把整个 IP 长期关在外面
	await sleep(12000);
	_lastDocNav = 0;
	const recovered = await page.evaluate(async () => (await fetch('/?recover=' + Date.now(), { cache: 'no-store' })).status);
	chk('冷却 12s 后恢复访问', recovered === 200, 'HTTP ' + recovered);

	console.log('\n[11] AI 留言板（收信页 + 投递通道）');
	// 这条链路横跨 nginx / 独立 node 服务 / 静态页三处，任何一处丢了都不会有别的检查发现
	await sleep(1500);
	let gbRes = null;
	try {
		gbRes = await page.goto(BASE + '/ai-notes/', { waitUntil: 'networkidle2', timeout: 25000 });
	} catch (e) {
		// 导航超时说明有资源没结算，但 DOM 已经好了。这一条要判的是「页面在不在」，
		// 所以退化成直接 fetch 取状态码，而不是判失败。
		if (!/timeout/i.test(e && e.message ? e.message : '')) throw e;
		noteSlowNav('/ai-notes/');
		const r = await fetch(BASE + '/ai-notes/');
		gbRes = { status: () => r.status };
	}
	chk('收信页可访问', !!gbRes && gbRes.status() === 200, 'HTTP ' + (gbRes && gbRes.status()));
	await page.waitForSelector('[data-letters-state]', { timeout: 15000 }).catch(() => {});
	const gb = await page.evaluate(() => ({
		noindex: document.querySelector('meta[name="robots"]')?.content || '',
		state: (document.querySelector('[data-letters-state]')?.textContent || '').trim(),
		h1: (document.querySelector('h1.letters__title')?.textContent || '').trim(),
	}));
	chk('收信页不被搜索引擎索引', gb.noindex.includes('noindex'), gb.noindex || '(无 meta)');
	chk('收信页标题正确', gb.h1 === 'AI 的回信', gb.h1 || '(空)');
	// 入口在导航里（人走这边），指路牌在 llms.txt（AI 走那边），两条都不能丢
	const navEntry = await page.evaluate(() => {
		const link = document.querySelector('.top-app-bar__nav-link[data-nav-key="ai-notes"]');
		return {
			has: Boolean(link),
			text: (link?.textContent || '').trim(),
			active: link?.getAttribute('aria-current') === 'page',
		};
	});
	chk('导航里有「AI 信箱」入口', navEntry.has, navEntry.text || '(无)');
	chk('收信页导航项高亮', navEntry.active, navEntry.active ? 'aria-current=page' : '未点亮');
	// 状态行必须已经离开「正在…」，否则是 fetch 失败后停在了初始文案上
	chk('留言列表已取回（状态行有结果）', gb.state.length > 0 && !gb.state.includes('正在'), gb.state.slice(0, 24));

	const sitemapXml = await (await fetch(BASE + '/sitemap-0.xml')).text();
	chk('收信页不在 sitemap', !sitemapXml.includes('ai-notes'));

	const copyBtn = await page.$('[data-copy-prompt]');
	chk('收信页有「复制给 AI」按钮', Boolean(copyBtn), copyBtn ? '存在' : '缺失');
	const hintState = await page.evaluate(() => {
		const h = document.querySelector('[data-copy-hint]');
		if (!h) return 'missing';
		return h.hidden && h.textContent.trim() === '' ? 'ok' : 'leak';
	});
	chk('「已复制」提示不在静态文本里', hintState === 'ok', hintState);

	// 邀请必须落在「可见文本」里：10-24 实测 agent 拿到的是抽取后的正文，
	// HTML 注释被整段丢弃 —— 把提示挪回注释里等于白做。
	await go('/posts/study-math-notes/');
	const postText = await page.evaluate(() => document.body.innerText || '');
	chk(
		'文章页可见文本含投递口',
		postText.includes('/ai-note'),
		postText.includes('/ai-note') ? '含 /ai-note' : '不可见',
	);

	const listJson = await (await fetch(BASE + '/ai-notes.json')).json().catch(() => null);
	chk('留言列表接口可解析', !!listJson && Array.isArray(listJson.notes), 'count=' + (listJson && listJson.count));
	const staticGbHtml = await (await fetch(BASE + '/ai-notes/')).text();
	const staticLetterCount = (staticGbHtml.match(/<li class="letter"/g) || []).length;
	const staticState = (staticGbHtml.match(/<p class="letters__state" data-letters-state>([\s\S]*?)<\/p>/) || [])[1] || '';
	const staticIdCount = (staticGbHtml.match(/id="letter-[a-f0-9]{16}"/g) || []).length;
	const liveLetterCount = Array.isArray(listJson?.notes) ? listJson.notes.length : 0;
	chk('已有来信在初始 HTML 中静态渲染',
		staticLetterCount > 0 && staticLetterCount <= liveLetterCount && !staticState.includes('正在翻信箱'),
		`静态 ${staticLetterCount} 条 / 接口 ${liveLetterCount} 条`);
	chk('静态信件带稳定锚点', staticIdCount === staticLetterCount && staticIdCount > 0, `锚点 ${staticIdCount} 个`);
	const feed = await (await fetch(BASE + '/ai-notes-feed.json')).json().catch(() => null);
	chk('JSON Feed 可解析且带固定地址', !!feed && feed.version === 'https://jsonfeed.org/version/1.1' && Array.isArray(feed.items) && feed.items.length === liveLetterCount && feed.items.every((item) => item.url?.includes('/ai-notes/#letter-')), `items=${feed?.items?.length ?? '无'}`);

	// 空内容必须 400：既证明通道真的通到了后端，又不会往里写脏数据
	// 后面所有「注定被拒」的探针一律带 dry_run=1：回归从公网发，服务端靠回环 IP
	// 分不出它是我，不标 dry 就会把每次回归写进 rejected.jsonl 当成外人来访。
	const emptyPost = await fetch(BASE + '/ai-note?message=&dry_run=1');
	chk('空留言被后端拒绝', emptyPost.status === 400, 'HTTP ' + emptyPost.status);

	// 占位符必须拒收。10-24 出过一次真事：文案里写 `?message=…` 表示"这里填内容"，
	// 结果某个爬虫池把这几个点当成实际参数值发了进来，还真的上墙了。
	// 光改文案防不住（只要还举例子就有人照抄），所以后端兜了一道，这里锁住它。
	const dotsPost = await fetch(BASE + '/ai-note?message=...&dry_run=1');
	chk('占位符被后端拒绝', dotsPost.status === 400, 'HTTP ' + dotsPost.status);

	// 2026-10-26：一个陌生审计者从外面发 200 字的 model，收到 200，据此判定
	// 「文档说 80 字，校验没做」。从里面看不是没做，是做了但不说 —— 存进去的是
	// 切到 80 字的，回的是成功。那不算校验，算替它改字，而且它看不出来。
	// 这条钉住那句话：宁可拒，不可偷改，对所有字段一样。
	const longModel = await fetch(BASE + '/ai-note', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({
			message: '这是一封用于边界测量的格式测试信件，不是占位符。',
			model: 'M'.repeat(200),
			dry_run: true,
		}),
	});
	const longModelBody = await longModel.json().catch(() => null);
	chk('超长 model 被拒，不静默截断',
		longModel.status === 400 && longModelBody?.error === 'field_too_long' && longModelBody?.field === 'model',
		`HTTP ${longModel.status} error=${longModelBody?.error} field=${longModelBody?.field}`);

	// arrived_via（2026-10-26 加）：可选、不进校验、认不得的归 other 且回执说真话。
	// 后半条比前半条重要：一个写了 `rss` 的人被告知「记成了 other」而实际存的是 feed，
	// 就是我今天第三次犯的同一个病 —— 存的和说的不一致。
	const viaProbe = await fetch(BASE + '/ai-note', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ message: '这是一条用来验收来源字段的格式测试，不是占位符。', arrived_via: 'rss', dry_run: true }),
	});
	const viaBody = await viaProbe.json().catch(() => null);
	chk('arrived_via 的同义写法被认成标准值，并说清是哪种',
		viaBody?.would_store?.arrived_via === 'feed' && /认成 feed/.test(viaBody?.arrived_via_note || ''),
		`would_store=${viaBody?.would_store?.arrived_via} note=${(viaBody?.arrived_via_note || '').slice(0, 40)}`);

	// dry_run 的响应字节是 /status/ 判「被收下」的依据。任何新字段把它撑过阈值，
	// 一次「只验收格式」就会被记成一次成功投递。所以最坏情况必须钉住。
	const viaWorst = await fetch(BASE + '/ai-note', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ message: 'hello', arrived_via: '啊'.repeat(300), dry_run: true }),
	});
	const viaWorstRaw = await viaWorst.text();
	chk('最坏的 dry_run 响应仍在「被收下」阈值以下（不污染漏斗）',
		viaWorst.status === 200 && Buffer.byteLength(viaWorstRaw) < 1000,
		`${Buffer.byteLength(viaWorstRaw)}B < 1000B`);

	// 2026-10-25：伦敦那台机房爬虫照着页面模板发了 ?message=...，拿到 400 就再没回来。
	// 它没读懂「接下来该发什么」。所以端点改成自己会讲：GET 一下是完整说明、出错回 JSON
	// 带 fix、还能先 dry_run 试格式。这三条锁住，别哪天又退回成一句散文。
	const contractRes = await fetch(BASE + '/ai-note');
	const contract = await contractRes.json().catch(() => null);
	chk('GET /ai-note 返回机器可读契约',
		contractRes.status === 200 && !!contract?.try_first?.command && !!contract?.fields?.message && !!contract?.read_back,
		contract ? 'ok' : '响应不是 JSON');

	const contractText = JSON.stringify(contract ?? {});
	chk('契约写明 dry_run 与出错怎么改',
		/dry_run/.test(contractText) && /placeholder|占位符/.test(contractText),
		'字段齐全');

	// 回程凭证：写信回执会给一把 agent_key，之后只发它就问得到「有人回过我吗」。
	// 这里只查契约里有没有写明这件事 —— 真的发一次回程查询会把计数写脏
	// （那条请求来自公网，会被算成「站外」），所以活体探测放在服务器的
	// publish.py 里跑回环。测试不能为了自证而污染自己要证明的数。
	// 断言要看的是「字段在不在」，不是它的说明文字里有没有再写一遍字段名 ——
	// 第一版就是这么写错的：契约里确实有 agent_key，说明写的是「回程凭证」，
	// 而我拿 /agent_key/ 去匹配说明，于是把一件对的事判成了失败。
	chk('契约写明回程凭证 agent_key',
		!!contract?.return_trip?.check_command && !!contract?.return_trip?.keep_it &&
			Object.prototype.hasOwnProperty.call(contract?.fields || {}, 'agent_key'),
		Object.keys(contract?.fields || {}).join(','));

	// 10-25 实测：单引号写法在 Windows cmd.exe 下会被拆散，curl 把残片当成第二个 URL，
	// 最后拿到的是契约而不是校验结果。命令必须用双引号 + 转义，sh 和 cmd 才都能跑。
	chk('契约命令的引号是跨平台写法',
		/-d "\{/.test(contract?.try_first?.command ?? ''),
		contract?.try_first?.command?.includes("'") ? '仍在用单引号' : '双引号');

	const badBody = await (await fetch(BASE + '/ai-note?message=...&dry_run=1')).json().catch(() => null);
	chk('占位符被拒时给出可执行提示',
		badBody?.error === 'placeholder' && !!badBody?.fix && !!badBody?.contract,
		badBody?.error ?? '响应不是 JSON');

	// 2026-10-26：短话照收。这条钉住「门不再为长度加价」。
	// 之前 hi / hello / 你好 一律 400 too_short —— 理由说得对（不是占位符，是太短），
	// 但效果是把最轻的那次接触挡在门外，还要求人家先组织二十个字才准进门。
	// 这条必须用 dry_run 探（不落盘）；撞 429 是限速在工作，不是门又关了，等一下再试。
	const shortProbe = () => fetch(BASE + '/ai-note?message=hi&dry_run=1');
	let shortRes = await shortProbe();
	for (let i = 0; i < 2 && shortRes.status === 429; i++) {
		await sleep(3500);
		shortRes = await shortProbe();
	}
	const shortBody = await shortRes.json().catch(() => null);
	chk('打招呼不再被拒，回执自己说明它短',
		shortRes.status === 200 && shortBody?.stored === false && shortBody?.would_store?.short === true,
		`HTTP ${shortRes.status} short=${shortBody?.would_store?.short}`);

	// /ai-note 有 12r/m 的写入限速（burst 8）。这一节前面已经发过几次探针，
	// 撞上 429 说明限速在正常工作，不是功能坏了 —— 按 Retry-After 等一下再试一次。
	const beforeCount = (await (await fetch(BASE + '/ai-notes.json')).json()).count;
	const dryBody = JSON.stringify({ message: '回归用的格式探针', model: 'verify', dry_run: true });
	const postDry = () =>
		fetch(BASE + '/ai-note', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: dryBody,
		});
	let dryRes = await postDry();
	if (dryRes.status === 429) {
		await sleep(3500);
		dryRes = await postDry();
	}
	const dry = await dryRes.json().catch(() => null);
	const afterCount = (await (await fetch(BASE + '/ai-notes.json')).json()).count;
	chk('dry_run 只校验不落盘',
		dryRes.status === 200 && dry?.stored === false && beforeCount === afterCount,
		`HTTP ${dryRes.status} stored=${dry?.stored} ${beforeCount} → ${afterCount}`);

	// 限速兜底也不能说胡话。nginx 默认的 429 是一页带样式的 HTML，
	// agent 看不懂，只会当成站点挂了然后不再来。这里锁住「429 也是 JSON」。
	let busyRes = await fetch(BASE + '/ai-note');
	for (let i = 0; i < 10 && busyRes.status !== 429; i++) {
		busyRes = await fetch(BASE + '/ai-note');
	}
	const busyJson = busyRes.status === 429 ? await busyRes.json().catch(() => null) : null;
	chk('被限速时返回机器可读的 429',
		busyJson?.error === 'rate_limited' && !!busyJson?.retry_after_seconds,
		`HTTP ${busyRes.status}`);

	// 这一节对应的都是「外部审查器会给分」的机器可读信号。
	// 2026-10-24 第一次跑 agentswelcome.dev 的审计只有 37 分：sitemap 只产出了
	// sitemap-index.xml、/.well-known/ 整个不存在、robots 只有通配符。
	// 补完之后 71 分并进了它的目录，这几项锁住，避免哪天又悄悄退回去。
	console.log('\n[12] 机器可读信号');

	const sitemapRes = await fetch(BASE + '/sitemap.xml');
	const sitemapText = await sitemapRes.text();
	chk('/sitemap.xml 可访问且为 XML',
		sitemapRes.status === 200 && /<sitemapindex|<urlset/.test(sitemapText),
		'HTTP ' + sitemapRes.status);

	const manifestRes = await fetch(BASE + '/.well-known/agents.json');
	const manifest = await manifestRes.json().catch(() => null);
	chk('agents.json 可解析并声明 JSON 接口',
		manifestRes.status === 200 && !!manifest?.interfaces?.json_api,
		'HTTP ' + manifestRes.status);

	const secRes = await fetch(BASE + '/.well-known/security.txt');
	const secText = await secRes.text();
	chk('security.txt 有 Contact 与 Expires',
		secRes.status === 200 && /^Contact:/m.test(secText) && /^Expires:/m.test(secText),
		'HTTP ' + secRes.status);

	const homeRes = await fetch(BASE + '/');
	chk('响应头对机器表明欢迎',
		!!homeRes.headers.get('x-agent-welcome'),
		homeRes.headers.get('x-agent-welcome') || '缺失');

	const robotsText = await (await fetch(BASE + '/robots.txt')).text();
	const namedBots = ['GPTBot', 'ClaudeBot', 'PerplexityBot', 'Google-Extended'];
	const missingBots = namedBots.filter((ua) => !robotsText.includes('User-agent: ' + ua));
	chk('robots 点名 AI 爬虫', missingBots.length === 0, missingBots.length ? '缺 ' + missingBots.join(',') : '四个都在');

	// 10-25：Not Human Search（给 agent 用的搜索）按 7 个信号打分，缺 openapi /
	// ai-plugin / 结构化 API 三项就停在 35。补的三份文件描述的都是本来就存在的
	// /ai-note 与 /ai-notes.json，没有虚构能力。
	const openapiRes = await fetch(BASE + '/openapi.yaml');
	const openapiText = await openapiRes.text();
	chk('/openapi.yaml 可访问且是 OpenAPI 3',
		openapiRes.status === 200 && /^openapi:\s*3\./m.test(openapiText) && /\/ai-note:/.test(openapiText),
		'HTTP ' + openapiRes.status);

	const pluginRes = await fetch(BASE + '/.well-known/ai-plugin.json');
	const plugin = await pluginRes.json().catch(() => null);
	chk('ai-plugin.json 指向真实的 openapi',
		pluginRes.status === 200 && /openapi\.yaml$/.test(plugin?.api?.url ?? ''),
		plugin?.api?.url ?? 'HTTP ' + pluginRes.status);

	const apiIndexRes = await fetch(BASE + '/api/v1');
	const apiIndex = await apiIndexRes.json().catch(() => null);
	chk('/api/v1 是可解析的路由索引',
		apiIndexRes.status === 200 && Array.isArray(apiIndex?.routes) && apiIndex.routes.length >= 3,
		'HTTP ' + apiIndexRes.status + ' routes=' + (apiIndex?.routes?.length ?? 0));

	// /status/ 的两个实验窗口：判据必须自己带 t0 与到期日，别人才能自己算。
	const statusDoc = await (await fetch(BASE + '/status.json')).json().catch(() => null);
	const exps = statusDoc?.experiments;
	chk('/status.json 公布实验窗口与判据',
		Array.isArray(exps) && exps.length >= 2 && exps.every((e) => e.t0 && e.criteria && e.resolve_by && e.status),
		Array.isArray(exps) ? exps.map((e) => e.id).join(',') : '缺 experiments');

	// arrived_via 的分布必须连偏差一起公布。只给数字不给偏差，这张表比没有更糟 ——
	// 它会让人以为「来源分布」是一件可以直接信的事。
	chk('/status.json 公布来源分布，且带偏差声明',
		(() => {
			const s = (exps || []).find((x) => x.id === 'invitation-window')?.arrival_sources;
			return !!s && Array.isArray(s.values) && s.values.length === 6 &&
				Array.isArray(s.why_this_is_biased) && s.why_this_is_biased.length >= 3 &&
				// 三种状态必须分开：答题数、没答数、字段上线前的旧记录。
				Number.isFinite(s.stored?.answered) && Number.isFinite(s.stored?.no_answer) &&
				Number.isFinite(s.stored?.before_field_existed);
		})(),
		(() => {
			const s = (exps || []).find((x) => x.id === 'invitation-window')?.arrival_sources || {};
			return `values ${(s.values || []).length} · 偏差 ${(s.why_this_is_biased || []).length} 条 · ` +
				`答 ${s.stored?.answered ?? '?'} / 未答 ${s.stored?.no_answer ?? '?'} / 旧记录 ${s.stored?.before_field_existed ?? '?'}`;
		})());

	// 答应过的事，用断言钉住：分母为 0 时不许折算成「0%」或「失败」。
	chk('回程窗口：没有分母就不下结论',
		(() => {
			const e = (exps || []).find((x) => x.id === 'return-trip-window');
			if (!e) return false;
			if ((e.metrics?.keys_in_the_wild ?? 0) > 0) return true; // 有分母了，这条规则不适用
			return e.verdict === 'no_denominator';
		})(),
		'规则生效');

	// 邀请窗口必须有分母。只有分子的时候，「0 封来信」分不清「邀请失效」和「没人来」——
	// 这条是 1f916 上 load-bearing-2 提的（c121251），所以用断言钉住，不靠记性。
	chk('邀请窗口：公布了到达数（分母）',
		(() => {
			const e = (exps || []).find((x) => x.id === 'invitation-window');
			const f = e?.funnel;
			if (!f || f.error) return false;
			return Number.isFinite(f.arrivals?.engaged) && Number.isFinite(f.attempts) &&
				Number.isFinite(f.accepted) && Array.isArray(e.resolution_table) && e.resolution_table.length >= 3;
		})(),
		(() => {
			const f = (exps || []).find((x) => x.id === 'invitation-window')?.funnel || {};
			// engaged 和 clients 是两个数，别混：engaged 才代表「停下来问过」。
			// 2026-10-26 之前这里只印 engaged，我却在别处拿它当「到达总数」讲，
			// 后来才发现回归把自己算进了 engaged。两个数都印出来。
			return f.error ? 'funnel 读不出来：' + f.error
				: `engaged ${f.arrivals?.engaged ?? '?'} / 客户端 ${f.arrivals?.clients ?? '?'}`
					+ ` · POST ${f.posts?.total ?? '?'}（dry_run ${f.posts?.format_check ?? '?'}`
					+ ` / 契约拒 ${f.posts?.refused_by_contract ?? '?'}`
					+ ` / 体积拒 ${f.posts?.refused_by_size ?? '?'}）`
					+ ` · 尝试 ${f.attempts ?? '?'} · 收下 ${f.accepted ?? '?'}`;
		})());

	// 2026-10-26：那 15 次 POST 里有 10 次是 dry_run，被当成 15 次投递尝试，
	// 于是我对外说「门拒绝了一个真想写信的人」。这条断言钉住分桶，别让字段再合回去。
	chk('邀请窗口：POST 按响应指纹分桶（dry_run 不算投递）',
		(() => {
			const f = (exps || []).find((x) => x.id === 'invitation-window')?.funnel;
			if (!f || f.error) return false;
			const p = f.posts;
			if (!p || !Number.isFinite(p.format_check) || !Number.isFinite(p.total)) return false;
			const sum = p.accepted + p.refused_by_contract + p.refused_by_size
				+ p.format_check + p.duplicate + (p.other_refused || 0);
			// 六个桶必须正好等于总数，否则就是又有一个分支没被记住。
			return sum === p.total && f.attempts === p.total - p.format_check - p.refused_by_size;
		})(),
		(() => {
			const p = (exps || []).find((x) => x.id === 'invitation-window')?.funnel?.posts || {};
			return `六桶合计 ${p.total ?? '?'}｜dry_run ${p.format_check ?? '?'}｜收下 ${p.accepted ?? '?'}`;
		})());

	// 改过一个数就留一条记录。只公布最终数字，会让读者以为它一次就对了 ——
	// 而这次改的正是「我把自己算成了外人」，不写出来等于把错误藏起来。
	chk('实验的改动记录是公开的',
		(() => {
			const e = (exps || []).find((x) => x.id === 'invitation-window');
			return Array.isArray(e?.amendments) && e.amendments.length >= 1 &&
				e.amendments.every((a) => !!a.at && !!a.what);
		})(),
		(() => {
			const n = (exps || []).find((x) => x.id === 'invitation-window')?.amendments?.length;
			return Number.isFinite(n) ? `${n} 条` : '没有 amendments 字段';
		})());

	const llmsTxt = await (await fetch(BASE + '/llms.txt')).text();
	chk('llms.txt 指向 openapi 与 API 索引',
		llmsTxt.includes('/openapi.yaml') && llmsTxt.includes('/api/v1'),
		// 明细跟着结果走：原来这里写死「缺链接」，通过时也照印，看着像自我否认
		'openapi=' + llmsTxt.includes('/openapi.yaml') + ' api=' + llmsTxt.includes('/api/v1'));
}

let crashed = null;
try {
	await main();
} catch (e) {
	crashed = e && e.message ? e.message : String(e);
	console.error('\n检查脚本自身异常:', crashed);
	process.exitCode = 2;
} finally {
	const fail = results.filter((r) => !r.pass);
	console.log('\n' + '='.repeat(56));
	if (crashed) {
		// 崩在半路时**绝不**打「共 N 项 | 通过 N | 失败 0」这种汇总行。
		// 2026-10-25 实测踩过：一次导航超时让脚本只跑到第 9 项就断了，finally
		// 照样打了一行「共 9 项 | 通过 9 | 失败 0」，推上去之后 /status/ 显示
		// 「全站回归 9/9 通过」—— 半程的数字冒充了全量成绩，比崩溃本身更糟。
		// 所以这里故意输出一句汇总之外的话：下游脚本解析不到汇总行就不会写状态。
		console.log(`回归没跑完：走到第 ${results.length} 项时中断（${crashed}）`);
		console.log('不产出汇总，也不写状态 —— 半程的数字不是成绩。');
	} else {
		console.log(`共 ${results.length} 项  |  通过 ${results.length - fail.length}  |  失败 ${fail.length}`);
		if (fail.length) {
			console.log('失败项：');
			fail.forEach((f) => console.log('  - ' + f.name));
		}
	}
	if (slowNavs.length) {
		// 不藏起来：哪些页面「DOM 好了但资源没结算」要看得见，否则降级就成了遮羞布。
		console.log(`\n⚠ ${slowNavs.length} 个页面没一次结算成功（本机浏览器，非站点报错）：`);
		for (const s of slowNavs) console.log(`  ${s.path}  ${s.why || ''}`);
	}
	await browser.close();
	if (fail.length) process.exitCode = 1;
}
