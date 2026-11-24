// 第十轮联调：真后端（ca_frozen 的快照）+ 新界面。第 1–4、6 节的接口一个都不拦截，只拦第 5 节（公司共享目录）的。
// 用法：CA_CODE=/home/claude/ca_frozen bash /home/claude/shot.sh r10 18995 /home/claude/home_ui10r /home/claude/tools/e2e_r10.js
// 资料：/home/claude/int_docs_r10/公司制度/（按部门分目录）。截图存 /home/claude/shots/r10_*.png
const { chromium } = require('playwright');
const fs = require('fs');
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const home = process.env.CA_HOME, port = process.env.CA_PORT;
  const docsDir = process.env.DOCS || '/home/claude/int_docs_r10/公司制度';
  const theme = process.env.CA_THEME || 'plain';
  const token = fs.readFileSync(home + '/token', 'utf8').trim();
  const base = 'http://127.0.0.1:' + port;
  const H = { 'X-Token': token, 'Content-Type': 'application/json', Origin: base };
  const api = async (m, p, b) => { const r = await fetch(base + p, { method: m, headers: H, body: b ? JSON.stringify(b) : undefined }); let j = null; try { j = await r.json(); } catch (e) {} return { s: r.status, j }; };
  const out = [], errs = [];
  const ok = (n, c, x) => out.push((c ? 'ok   ' : 'FAIL ') + n + (x !== undefined ? '  ' + String(typeof x === 'string' ? x : JSON.stringify(x)).replace(/\s+/g, ' ').slice(0, 300) : ''));
  const shot = (p, n) => p.screenshot({ path: '/home/claude/shots/r10_' + theme + '_' + n + '.png' });
  const now = Date.now() / 1000;

  // ---- 准备：建库收录 ----
  await api('POST', '/api/settings', { ui: { theme } });
  let lib = ((await api('GET', '/api/libraries')).j || []).find(l => l.name === '公司制度');
  if (!lib) {
    lib = (await api('POST', '/api/libraries', { name: '公司制度', note: '各部门的制度' })).j;
    await api('POST', '/api/libraries/' + lib.id + '/sources', { path: docsDir });
  }
  for (let i = 0; i < 80; i++) { const L = ((await api('GET', '/api/libraries')).j || []).find(x => x.id === lib.id); if (L && L.state === 'ready' && L.docs >= 9) { lib = L; break; } await sleep(500); }
  const docs = (await api('GET', '/api/libraries/' + lib.id + '/docs')).j || [];
  const byTitle = t => docs.find(d => d.title.indexOf(t) >= 0);
  const st0 = (await api('GET', '/api/state')).j;
  const wsId = st0.default_workspace_id || (st0.workspaces[0] || {}).id;

  // 第 5 节（公司共享目录）现在也是真的了：一个接口都不拦截
  const browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox'] });
  async function open(hash, w, opt) {
    opt = opt || {};
    const b0 = opt.base || base, t0 = opt.token || token;
    const ctx = await browser.newContext({ viewport: { width: w || 1440, height: 900 } });
    if (!opt.keepTheme) await ctx.addInitScript(t => { try { localStorage.setItem('ca.theme', t); } catch (e) {} }, theme);
    const page = await ctx.newPage();
    page.on('pageerror', e => errs.push('pageerror: ' + String(e).slice(0, 200)));
    page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errs.push('console: ' + m.text().slice(0, 200)); });
    page.on('response', r => { if (r.status() >= 400 && !(opt.expect && opt.expect.test(r.url()))) errs.push(r.status() + ' ' + r.request().method() + ' ' + r.url().replace(b0, '').replace(/[?&]t=[^&]+/, '')); });
    await page.goto(b0 + '/?t=' + t0 + hash, { waitUntil: 'networkidle' });
    await page.waitForTimeout(700);
    return { page, ctx };
  }
  const txt = (p, sel) => p.$eval(sel, e => e.innerText.replace(/\s+/g, ' ')).catch(() => '');
  const waitIdle = async sid => { for (let i = 0; i < 40; i++) { const s = (await api('GET', '/api/sessions/' + sid)).j; if (s && s.session && !/running|waiting/.test(s.session.status)) return s; await sleep(400); } return (await api('GET', '/api/sessions/' + sid)).j; };
  const LIB = '#/hspace/kb/' + encodeURIComponent(lib.id);

  // ===== 1. 文档表：摘要（开头摘录）、标签；资料库那一行说明摘要的状态 =====
  { const { page, ctx } = await open(LIB);
    const sums = await page.$$eval('.doc-sum', e => e.map(x => x.innerText));
    ok('1.1 文档表有摘要第一行，标"开头摘录"', sums.length >= 8 && sums.filter(s => /开头摘录/.test(s)).length >= 8, sums.slice(0, 2));
    const tags = await page.$$eval('.doc-tags .tag', e => e.map(x => x.textContent));
    ok('1.2 文档表有标签（财务部、报销）', tags.includes('财务部') && tags.includes('报销'), [...new Set(tags)]);
    const cards = await txt(page, '.lib-cards'), why = await txt(page, '.lib-cards-why');
    ok('1.3 资料库那一行说明摘要的状态（不说"后台在写"）', /开头摘录/.test(cards) && !/后台在写/.test(cards) && /演示模式/.test(why), cards + ' | ' + why);
    ok('1.4 没有跳动的"在写"小点', !(await page.$('.lib-cards.is-busy')));
    await page.click('.tag-filter .chip >> text=报销'); await page.waitForTimeout(200);
    ok('1.5 点标签筛文档', /显示 3 \/ 9 份/.test(await txt(page, '.doc-count')), await txt(page, '.doc-count'));
    await page.click('.tag-filter .chip >> text=报销'); await page.waitForTimeout(100);
    await page.evaluate(() => document.querySelector('.doc-filter').scrollIntoView()); await page.waitForTimeout(150);
    await shot(page, '01_doc_table');
    await ctx.close(); }

  // ===== 2. 文档卡片：目录、看一节、改标签、改摘要，刷新后还在 =====
  const buy = byTitle('采购审批权限');
  { const { page, ctx } = await open(LIB);
    await page.click('.doc-open >> text=采购审批权限管理办法'); await page.waitForTimeout(600);
    const items = await page.$$eval('.dc-oi', e => e.map(x => x.innerText.replace(/\s+/g, ' ')));
    ok('2.1 卡片的目录有内容', items.length >= 2, items.slice(0, 5));
    await page.click('.dc-oi:not([disabled]) >> nth=1'); await page.waitForTimeout(500);
    const sect = await txt(page, '.dc-sect:not([hidden])');
    ok('2.2 点目录一项看到那一节的原文', sect.length > 10 && !/没有读到/.test(sect), sect.slice(0, 80));
    await page.fill('.tag-input', '采购'); await page.press('.tag-input', 'Enter'); await page.waitForTimeout(500);
    await page.click('.dc-edit'); await page.waitForTimeout(200);
    await page.fill('.dc-sec textarea', '采购审批权限：5 万以下部门负责人，5 万到 50 万分管副总裁，50 万以上总裁办公会。');
    await page.click('.dc-sec button[type=submit]'); await page.waitForTimeout(500);
    await shot(page, '02_card');
    await ctx.close(); }
  { const { page, ctx } = await open(LIB);
    const row = await page.$$eval('.doc-row', e => e.map(x => x.innerText.replace(/\s+/g, ' ')).find(t => /采购审批/.test(t)) || '');
    ok('2.3 刷新后文档表里标签和摘要都在', /采购/.test(row) && /总裁办公会/.test(row) && !/开头摘录/.test(row), row.slice(0, 160));
    await page.click('.doc-open >> text=采购审批权限管理办法'); await page.waitForTimeout(600);
    const card = await txt(page, '#doc-card');
    ok('2.4 刷新后卡片里还在，摘要标"你改过的"', /你改过的/.test(card) && /总裁办公会/.test(card) && /采购×|采购 ×/.test(card), card.slice(0, 200));
    const c = (await api('GET', '/api/kb/docs/' + buy.id + '/card')).j;
    ok('2.5 后端存住了（summary_by=user，tags 带"采购"）', c.summary_by === 'user' && c.tags.includes('采购'), { by: c.summary_by, tags: c.tags });
    await page.keyboard.press('Escape'); await page.waitForTimeout(200);
    ok('2.6 Esc 关上卡片', await page.$eval('#doc-card', e => e.hidden));
    await ctx.close(); }

  // ===== 3. 范围：指定文件、标签、# =====
  let scopedSid = '';
  { const { page, ctx } = await open('#/hspace');
    let body = null;
    page.on('request', r => { if (r.method() === 'POST' && /\/api\/sessions(\?|$)/.test(r.url())) body = JSON.parse(r.postData() || '{}'); });
    await page.click('#home-kb .scope-btn'); await page.waitForTimeout(250);
    await page.click('#scope-menu >> text=指定文件…'); await page.waitForTimeout(600);
    await page.keyboard.type('采购'); await page.waitForTimeout(600);
    const found = await page.$$eval('#doc-pop .dp-item .mi-label', e => e.map(x => x.textContent));
    ok('3.1 指定文件…：按名字找到（真接口 /api/kb/docs?q=）', found[0] && /采购审批权限/.test(found[0]), found);
    await page.keyboard.press('Enter'); await page.waitForTimeout(300);
    await page.keyboard.press('Escape'); await page.waitForTimeout(200);
    await page.click('#home-kb .scope-btn'); await page.waitForTimeout(250);
    await page.click('#scope-menu .scope-tag >> text=报销'); await page.waitForTimeout(250);
    await page.keyboard.press('Escape'); await page.waitForTimeout(200);
    await page.click('#home-input'); await page.keyboard.type('#差旅管理办法（2026'); await page.waitForTimeout(800);
    const hashRows = await page.$$eval('#hash-menu .slash-opt .slash-name', e => e.map(x => x.textContent));
    ok('3.2 # 弹出文件列表', hashRows.length >= 1 && /2026/.test(hashRows[0]), hashRows);
    await page.keyboard.press('Enter'); await page.waitForTimeout(300);
    const chips = await page.$$eval('#home-scope .sc-chip .sc-text', e => e.map(x => x.textContent));
    ok('3.3 输入框上方三个小标签', chips.length === 3, chips);
    await page.keyboard.type('出差住宿标准是多少？'); await shot(page, '03_scope');
    await page.keyboard.press('Enter'); await page.waitForTimeout(2500);
    ok('3.4 发出去的请求带 docs 和 tags', body && body.docs && body.docs.length === 2 && body.tags && body.tags[0] === '报销', body && { docs: body.docs, tags: body.tags, libraries: body.libraries });
    scopedSid = decodeURIComponent((page.url().match(/task\/([^/?#]+)/) || [])[1] || '');
    const s = await waitIdle(scopedSid);
    ok('3.5 对话的 meta 里存了范围', s && s.session && (s.session.meta.docs || []).length === 2 && (s.session.meta.tags || [])[0] === '报销', s && s.session && { docs: s.session.meta.docs, tags: s.session.meta.tags });
    const hitTitles = [];
    (s.events || []).forEach(e => { if (e.type === 'tool_result' && Array.isArray(e.payload.hits)) e.payload.hits.forEach(h => { if (!h.standard) hitTitles.push(h.title); }); });
    // 指定的文件和标签之间是"或"：点了名的两份一定查，带"报销"标签的（差旅 2025 / 2026、常见问题）也带上
    const inScope = t => /采购审批权限管理办法|差旅管理办法（2026 版）|差旅管理办法（2025 版）|常见问题/.test(t);
    ok('3.6 检索只在范围里（点名的两份 + 带"报销"标签的）', hitTitles.length > 0 && hitTitles.every(inScope), [...new Set(hitTitles)]);
    const chipsTask = await page.$$eval('#composer-scope .sc-chip .sc-text', e => e.map(x => x.textContent)).catch(() => []);
    ok('3.7 对话页输入框上方也显示范围（文件名补出来了）', chipsTask.length === 3 && chipsTask.some(t => /采购/.test(t)), chipsTask);
    const r = (await api('POST', '/api/kb/search', { query: '采购审批权限', docs: body.docs, tags: ['报销'] })).j;
    const ht = (r.hits || []).filter(h => !h.standard).map(h => h.title);
    const r2 = (await api('POST', '/api/kb/search', { query: '带薪年假', docs: body.docs, tags: ['报销'] })).j;
    const ht2 = (r2.hits || []).filter(h => !h.standard).map(h => h.title);
    ok('3.8 检索测试接口同样只在范围里（点名的采购办法查得到；范围外的休假规定查不到）', ht.length > 0 && ht.every(inScope) && ht.some(t => /采购/.test(t)) && !ht2.some(t => /休假/.test(t)), { 采购审批权限: ht, 带薪年假: ht2 });
    await ctx.close(); }

  // ===== 4. 没答上来的问题 → 写答案 → 标准答案 → 再问用上 =====
  const Q = '公司给员工养宠物有补贴吗？';
  { const { page, ctx } = await open('#/hspace');
    await page.fill('#home-input', Q); await page.press('#home-input', 'Enter'); await page.waitForTimeout(2500);
    const sid = decodeURIComponent((page.url().match(/task\/([^/?#]+)/) || [])[1] || '');
    await waitIdle(sid);
    await ctx.close(); }
  await sleep(600);
  let gaps = (await api('GET', '/api/kb/gaps?status=open')).j || [];
  const gap = gaps.find(g => /宠物/.test(g.question));
  ok('4.1 资料里没有的问题进了"没答上来的问题"', !!gap, gaps.map(g => g.question + '·' + g.reason + '·' + g.count));
  { const { page, ctx } = await open('#/hspace/kb');
    const gq = await page.$$eval('.gap-q', e => e.map(x => x.textContent));
    ok('4.2 知识库页显示它，标题旁有个数', gq.some(t => /宠物/.test(t)) && /\d/.test(await txt(page, '.count-pill')), { gq, n: await txt(page, '.count-pill') });
    await page.evaluate(() => { const t = [...document.querySelectorAll('.panel-title')].find(x => /没答上来/.test(x.textContent)); if (t) t.scrollIntoView(); });
    await shot(page, '04_gaps');
    const i = gq.findIndex(t => /宠物/.test(t));
    await page.click('.gap >> nth=' + i + ' >> text=写答案'); await page.waitForTimeout(200);
    await page.keyboard.type('公司没有养宠物补贴。员工福利见《员工休假管理规定》和年度福利清单。');
    await page.click('.gap form button[type=submit]'); await page.waitForTimeout(800);
    ok('4.3 写答案后那条从列表里消失', !(await page.$$eval('.gap-q', e => e.map(x => x.textContent))).some(t => /宠物/.test(t)));
    await ctx.close(); }
  gaps = (await api('GET', '/api/kb/gaps')).j || [];
  const g2 = gaps.find(g => /宠物/.test(g.question));
  ok('4.4 后端：那条标成 done，answer_id 指过去', g2 && g2.status === 'done' && !!g2.answer_id, g2 && { status: g2.status, answer_id: g2.answer_id });
  const ans0 = ((await api('GET', '/api/kb/answers')).j || []).find(a => /宠物/.test(a.question));
  ok('4.5 变成了标准答案（还没用过）', ans0 && ans0.hits === 0, ans0 && { q: ans0.question, hits: ans0.hits });
  { const { page, ctx } = await open('#/hspace');
    await page.fill('#home-input', Q); await page.press('#home-input', 'Enter'); await page.waitForTimeout(2500);
    const sid = decodeURIComponent((page.url().match(/task\/([^/?#]+)/) || [])[1] || '');
    const s = await waitIdle(sid); await page.waitForTimeout(500);
    const ans = (s.events || []).filter(e => e.type === 'assistant_message').map(e => e.payload.content).join(' ');
    ok('4.6 再问一遍：按标准答案回答', /没有养宠物补贴/.test(ans), ans.slice(0, 120));
    const badges = await page.$$eval('.kb-badge', e => e.map(x => x.textContent)).catch(() => []);
    await ctx.close(); }
  await sleep(500);
  const ans1 = ((await api('GET', '/api/kb/answers')).j || []).find(a => /宠物/.test(a.question));
  ok('4.7 "用过 N 次"变了', ans1 && ans1.hits >= 1, ans1 && { hits: ans1.hits, last_hit: ans1.last_hit });
  { const { page, ctx } = await open('#/hspace/kb');
    const uses = await page.$$eval('.ans', e => e.map(x => x.innerText.replace(/\s+/g, ' ')).filter(t => /宠物/.test(t)));
    ok('4.8 知识库页标准答案写"用过 1 次"', uses.some(t => /用过 \d+ 次/.test(t)), uses);
    await ctx.close(); }

  // ===== 5. 检索评测 =====
  { const { page, ctx } = await open('#/hspace/kb');
    await page.click('details.eval > summary'); await page.waitForTimeout(200);
    const add = async (q, e) => {
      await page.click('details.eval button:has-text("加一题")'); await page.waitForTimeout(150);
      await page.fill('details.eval form input >> nth=0', q); await page.fill('details.eval form input >> nth=1', e);
      await page.click('details.eval form button[type=submit]'); await page.waitForTimeout(500);
    };
    await add('50 万以上的采购谁审批', '采购审批权限管理办法');
    await add('公司数据分几个密级', 'Global Data Classification Policy');
    const items = await page.$$eval('.eval-table tbody tr', e => e.length);
    ok('5.1 加了两题', items === 2, items);
    await page.click('details.eval button:has-text("跑一遍")'); await page.waitForTimeout(1200);
    const score = await txt(page, '.eval-score'), miss = await txt(page, '.eval-miss'), meta = await txt(page, '.eval-res p.small');
    ok('5.2 跑一遍：命中率和没命中的那题（中文问英文文件）', /前 1 条命中 50%/.test(score) && /密级/.test(miss) && /应该是/.test(miss), score + ' | ' + miss.slice(0, 120) + ' | ' + meta);
    ok('5.3 方式写实际用的（只用了关键词）', /只用了关键词/.test(meta), meta);
    await page.click('details.eval button:has-text("跑一遍")'); await page.waitForTimeout(1200);
    const score2 = await txt(page, '.eval-score');
    ok('5.4 再跑一遍能和上次比', /和上次一样|比上次/.test(score2), score2);
    await page.evaluate(() => { const t = document.querySelector('details.eval'); if (t) t.scrollIntoView(); }); await page.waitForTimeout(150);
    await shot(page, '05_eval');
    await ctx.close(); }
  { const { page, ctx } = await open('#/hspace/kb');
    await page.click('details.eval > summary'); await page.waitForTimeout(800);
    const miss = await txt(page, '.eval-miss');
    ok('5.5 重新打开页面：上一次的没命中明细也在（/api/kb/eval/runs/<id>）', /密级/.test(miss), miss.slice(0, 100));
    await ctx.close(); }

  // ===== 6. 模板 =====
  { const { page, ctx } = await open('#/hspace');
    const nSess = ((await api('GET', '/api/sessions')).j || []).length;
    await page.click('.tpl-chip >> text=两版预算表对一下'); await page.waitForTimeout(400);
    const labels = await page.$$eval('#home-tplform .tf-label', e => e.map(x => x.textContent));
    ok('6.1 点带参数的内置模板 → 表单', labels.length === 2, labels);
    await page.click('#home-tplform button[type=submit]'); await page.waitForTimeout(300);
    ok('6.2 必填不填有提示', (await page.$$eval('#home-tplform [aria-invalid=true]', e => e.length)) === 2);
    await shot(page, '06_tpl_form');
    await page.fill('#home-tf-0', '预算表_v1.xlsx'); await page.fill('#home-tf-1', '预算表_v2.xlsx');
    await page.press('#home-tf-1', 'Enter'); await page.waitForTimeout(600);
    const v = await page.$eval('#home-input', e => e.value);
    ok('6.3 填好后文字进了输入框（后端 render），没有自动发送', /预算表_v1\.xlsx/.test(v) && /预算表_v2\.xlsx/.test(v) && /#\/hspace$|#\/hspace\/?$/.test(page.url()) && ((await api('GET', '/api/sessions')).j || []).length === nSess, v);
    await page.fill('#home-input', ''); await page.click('#home-input');
    await page.keyboard.type('/两版预算表对一下'); await page.waitForTimeout(400); await page.keyboard.press('Enter'); await page.waitForTimeout(400);
    ok('6.4 斜杠指令里同样弹表单', !(await page.$eval('#home-tplform', e => e.hidden)) && (await page.$eval('#home-input', e => e.value)) === '');
    await page.keyboard.press('Escape'); await page.waitForTimeout(200);
    ok('6.5 Esc 收起表单，焦点回到输入框', (await page.$eval('#home-tplform', e => e.hidden)) && (await page.evaluate(() => document.activeElement.id)) === 'home-input');
    await ctx.close(); }
  { const { page, ctx } = await open('#/new');
    await page.click('.tpl-more'); await page.waitForTimeout(200);
    const nr = (await page.$$('.tpl-role')).length;
    for (let i = 0; i < nr; i++) { await page.click('.tpl-role >> nth=' + i); await page.waitForTimeout(100); if (await page.$('.tpl >> text=经营数据看一眼')) break; }
    await page.click('.tpl >> text=经营数据看一眼').catch(() => {}); await page.waitForTimeout(400);
    const has = !(await page.$eval('#home-tplform', e => e.hidden).catch(() => true));
    if (has) {
      await page.fill('#home-tf-0', '2026-03'); await page.press('#home-tf-0', 'Enter').catch(() => {}); await page.waitForTimeout(600);
    }
    const v6 = await page.$eval('#home-input', e => e.value).catch(() => '');
    ok('6.6 CariAi 的月份模板：表单填年月，文字进输入框', has && /2026-03/.test(v6), v6);
    await ctx.close(); }
  { const { page, ctx } = await open('#/hspace/settings');
    await page.click('button:has-text("新建模板")'); await page.waitForTimeout(200);
    await page.keyboard.type('客户简报'); await page.keyboard.press('Tab');
    await page.keyboard.type('给 {{客户名称}} 写一页简报，{{重点:choice=回款|续约}}，{{备注?}}，{{坏的:week}}');
    await page.waitForTimeout(900);
    const live = await txt(page, '.tpl-live');
    ok('6.7 "我的模板"实时显示解析结果（后端 parse 的报错）', /解析出 3 个空/.test(live) && /week/.test(live), live);
    await page.fill('.mytpl-edit textarea', '给 {{客户名称}} 写一页简报，{{重点:choice=回款|续约}}，{{备注?}}');
    await page.click('.mytpl-edit button[type=submit]'); await page.waitForTimeout(800);
    ok('6.8 存好后出现在列表里', (await txt(page, '.mytpl-list')).indexOf('客户简报') >= 0, await txt(page, '.mytpl-list'));
    await ctx.close(); }
  { const { page, ctx } = await open('#/hspace');
    await page.click('.tpl-more'); await page.waitForTimeout(300);
    let all = [];
    const nr2 = (await page.$$('.tpl-role')).length;
    for (let i = 0; i < nr2; i++) { await page.click('.tpl-role >> nth=' + i); await page.waitForTimeout(100); all = all.concat(await page.$$eval('.tpl-title', e => e.map(x => x.innerText))); }
    const sug = await page.$$eval('.tpl-chip', e => e.map(x => x.innerText));
    ok('6.9 新建的模板出现在建议里（"我的"）', all.some(t => /客户简报/.test(t)) || sug.some(t => /客户简报/.test(t)), sug.concat(all).filter(t => /客户|我的/.test(t)));
    await ctx.close(); }
  { const { page, ctx } = await open('#/hspace/settings');
    page.on('dialog', d => d.accept());
    await page.click('.mytpl >> text=删'); await page.waitForTimeout(800);
    const t = ((await api('GET', '/api/templates')).j || []).filter(x => x.source === 'user');
    ok('6.10 能删', t.length === 0 && !/客户简报/.test(await txt(page, '.mytpl-list')), t.map(x => x.title));
    await ctx.close(); }

  // ===== 7. /状态 的工具行 =====
  const statusOf = async hash => {
    const { page, ctx } = await open(hash);
    await page.fill('#composer-input', '/状态'); await page.press('#composer-input', 'Enter'); await page.waitForTimeout(900);
    await page.click('.ts-det > summary').catch(() => {}); await page.waitForTimeout(200);
    const line = await txt(page, '.ts-det');
    return { page, ctx, line };
  };
  const cs = (await api('POST', '/api/sessions', { workspace_id: wsId, mode: 'cariai', text: '你好' })).j;
  await waitIdle(cs.id);
  { const r = await statusOf('#/task/' + cs.id);
    ok('7.1 CariAi /状态：工具一行是真数字', /带了 \d+ 组（约 [\d,]+ 字符）/.test(r.line), r.line.slice(0, 160));
    await r.ctx.close(); }
  { const r = await statusOf('#/hspace/task/' + scopedSid);
    ok('7.2 Hspace /状态：工具一行是真数字，组名用后端的', /带了 \d+ 组（约 [\d,]+ 字符）/.test(r.line) && /查资料库/.test(r.line), r.line.slice(0, 200));
    await shot(r.page, '07_status');
    await r.ctx.close(); }
  const tsC = (await api('GET', '/api/sessions/' + cs.id + '/status')).j.toolset, tsH = (await api('GET', '/api/sessions/' + scopedSid + '/status')).j.toolset;
  ok('7.3 后端的字符数', tsC && tsH, { cariai: tsC && tsC.chars, hspace: tsH && tsH.chars, all: tsC && tsC.chars_all });

  // ===== 8. CariAi 打开网页 → 批准卡片 =====
  { const { page, ctx } = await open('#/new');
    await page.fill('#home-input', '打开网页 https://example.com'); await page.press('#home-input', 'Enter');
    let card = null;
    for (let i = 0; i < 20 && !card; i++) { await page.waitForTimeout(500); card = await page.$('.approve'); }
    ok('8.1 CariAi 说"打开网页"仍然出现批准卡片', !!card, card ? await txt(page, '.approve') : '');
    const sid = decodeURIComponent((page.url().match(/task\/([^/?#]+)/) || [])[1] || '');
    const steps = ((await api('GET', '/api/sessions/' + sid)).j.events || []).filter(e => e.type === 'tool_started' || e.type === 'approval_requested').map(e => e.payload.tool);
    await shot(page, '08_open_web');
    await ctx.close(); }

  // ===== 9. 第 5 节：公司共享目录（真后端；一个新的数据目录当"普通员工"） =====
  const { execSync, spawn } = require('child_process');
  const SH = '/home/claude/shared_e2e', SH_SRC = '/home/claude/shared_demo';
  // 干净的一份：上一次跑剩下的整个挪到 shared_e2e_runs/ 下（不删文件），再从 shared_demo 复制
  if (fs.existsSync(SH)) { fs.mkdirSync('/home/claude/shared_e2e_runs', { recursive: true }); fs.renameSync(SH, '/home/claude/shared_e2e_runs/' + Date.now().toString(36)); }
  execSync('cp -r ' + JSON.stringify(SH_SRC) + ' ' + JSON.stringify(SH));
  const t9 = Date.now() / 1000 - 2;
  const sHome = '/home/claude/home_ui10_staff_' + Date.now().toString(36);
  fs.mkdirSync(sHome, { recursive: true });
  const srv = spawn('python3', ['-m', 'agent', '--port', '18997', '--home', sHome, '--mock', '--no-open'],
    { cwd: process.env.CA_CODE || '/home/claude/ca_frozen', env: Object.assign({}, process.env, { PYTHONDONTWRITEBYTECODE: '1' }), stdio: ['ignore', 'pipe', 'pipe'] });
  let logTxt = ''; srv.stdout.on('data', d => { logTxt += d; }); srv.stderr.on('data', d => { logTxt += d; });
  let sPort = '';
  for (let i = 0; i < 80 && !(sPort && fs.existsSync(sHome + '/token')); i++) { await sleep(250); const m = /127\.0\.0\.1:(\d+)/.exec(logTxt); if (m) sPort = m[1]; }
  const sBase = 'http://127.0.0.1:' + sPort, sTok = fs.readFileSync(sHome + '/token', 'utf8').trim();
  const sH = { 'X-Token': sTok, 'Content-Type': 'application/json', Origin: sBase };
  const sapi = async (m, p, b) => { const r = await fetch(sBase + p, { method: m, headers: sH, body: b ? JSON.stringify(b) : undefined }); let j = null; try { j = await r.json(); } catch (e) {} return { s: r.status, j }; };
  for (let i = 0; i < 40; i++) { try { if ((await sapi('GET', '/api/state')).s === 200) break; } catch (e) {} await sleep(250); }
  const sopen = (hash, opt) => open(hash, 1440, Object.assign({ base: sBase, token: sTok }, opt || {}));
  const sWs = (await sapi('GET', '/api/state')).j.default_workspace_id;
  const sIdle = async sid => { for (let i = 0; i < 40; i++) { const s = (await sapi('GET', '/api/sessions/' + sid)).j; if (s && s.session && !/running|waiting/.test(s.session.status)) return s; await sleep(400); } return (await sapi('GET', '/api/sessions/' + sid)).j; };
  // 公司默认的批准方式是"每步都问"：Hspace 查资料也要点批准
  const askHs = async (page, q) => {
    await page.fill('#home-input', q); await page.press('#home-input', 'Enter'); await page.waitForTimeout(1500);
    const sid = decodeURIComponent((page.url().match(/task\/([^/?#]+)/) || [])[1] || '');
    let approved = 0;
    for (let i = 0; i < 40; i++) {
      const b = await page.$('.approve button:has-text("批准")');
      if (b) { await b.click().catch(() => {}); approved++; await page.waitForTimeout(800); continue; }
      const s = (await sapi('GET', '/api/sessions/' + sid)).j;
      if (s && s.session && !/running|waiting/.test(s.session.status)) break;
      await page.waitForTimeout(400);
    }
    return { sid, approved, s: await sIdle(sid) };
  };

  // 9.1 设置页填共享目录、保存并检查（这一张是深色：公司默认主题是墨夜）
  { const { page, ctx } = await sopen('#/settings', { keepTheme: true });
    const sec = async () => page.evaluate(() => { const t = [...document.querySelectorAll('.panel-title')].find(x => x.textContent === '公司共享目录'); return t ? t.closest('section').innerText.replace(/\s+/g, ' ') : ''; });
    ok('9.0 没配时显示"没有设置"', /没有设置/.test(await sec()), (await sec()).slice(0, 120));
    await page.fill('input[aria-label="共享目录的路径"]', SH);
    await page.click('button:has-text("保存并检查")'); await page.waitForTimeout(2500);
    const t = await sec();
    ok('9.1 连上了、公司名和联系人', /连上了：示例集团/.test(t) && /IT 服务台/.test(t), t.slice(0, 160));
    ok('9.2 内容：2 个技能、3 条模板、1 个资料库和它的版本', /2 个技能 · 3 条模板 · 1 个资料库/.test(t) && /公司制度/.test(t) && /第 3 版/.test(t), (t.match(/内容[^。]*?资料库/) || [''])[0] + ' | ' + (t.match(/公司制度[^|]{0,40}/) || [''])[0]);
    const ign = await page.$$eval('.shared-ignored li', e => e.map(x => x.innerText.replace(/\s+/g, ' ')));
    ok('9.3 "没有用上的公司设置"列出 5 项和原因', ign.length === 5 && ign.every(x => x.split(' ').length > 1), ign);
    await page.evaluate(() => { const t = [...document.querySelectorAll('.panel-title')].find(x => x.textContent === '公司共享目录'); if (t) t.scrollIntoView(); }); await page.waitForTimeout(200);
    await page.screenshot({ path: '/home/claude/shots/r10s_dark_01_shared.png' });
    await ctx.close(); }
  // 9.2 公司默认的主题生效
  { const { page, ctx } = await sopen('#/hspace', { keepTheme: true });
    await page.waitForTimeout(800);
    const th = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));
    ok('9.4 公司给的默认值生效（主题变成墨夜）', th === 'ink-night', th);
    await ctx.close(); }
  // 9.3 锁住的项
  { const r = await sapi('POST', '/api/settings', { kb: { min_sim: 0.2 } });
    ok('9.5 接口直接改锁住的项 → 400', r.s === 400 && /公司统一设置/.test((r.j && r.j.error) || ''), r.j);
    const st = (await sapi('GET', '/api/settings')).j;
    ok('9.6 GET /api/settings 带 locked', (st.locked || []).includes('kb.min_sim') && (st.locked || []).includes('browser.allowed_domains'), st.locked); }
  // 从这里起本机把主题改回浅色（本机改过的以本机为准）
  await sapi('POST', '/api/settings', { ui: { theme: 'plain' } });
  { const { page, ctx } = await sopen('#/settings');
    const locked = await page.$$eval('.panel.is-locked', e => e.map(x => (x.querySelector('.panel-title') || {}).textContent + '｜' + (x.querySelector('.locked-note') || {}).textContent));
    const dis = await page.$$eval('.panel.is-locked input, .panel.is-locked select, .panel.is-locked textarea', e => e.every(x => x.disabled));
    ok('9.7 被锁住的设置项置灰并标"由公司统一设置"', locked.length >= 1 && locked.every(x => /由公司统一设置/.test(x)) && dis, locked);
    await page.evaluate(() => { const t = document.querySelector('.panel.is-locked'); if (t) t.scrollIntoView(); }); await page.waitForTimeout(200);
    await page.screenshot({ path: '/home/claude/shots/r10s_plain_02_locked.png' });
    await page.evaluate(() => { const t = [...document.querySelectorAll('.panel-title')].find(x => x.textContent === '公司共享目录'); if (t) t.scrollIntoView(); }); await page.waitForTimeout(200);
    await page.screenshot({ path: '/home/claude/shots/r10s_plain_01_shared.png' });
    await ctx.close(); }
  // 9.4 知识库页：公司的库
  const coLib = ((await sapi('GET', '/api/libraries')).j || []).find(l => l.shared);
  { const { page, ctx } = await sopen('#/hspace/kb', { keepTheme: false });
    const row = await page.$$eval('.lib-row', e => e.map(x => x.innerText.replace(/\s+/g, ' ')).find(t => /公司/.test(t) && /版/.test(t)) || '');
    ok('9.8 知识库页出现公司的库，带"公司"标记和版本', /公司制度/.test(row) && /第 3 版/.test(row), row);
    await page.evaluate(() => document.querySelector('.lib-list').scrollIntoView()); await page.waitForTimeout(150);
    await page.screenshot({ path: '/home/claude/shots/r10s_plain_03_kb_company.png' });
    await ctx.close(); }
  { const { page, ctx } = await sopen('#/hspace/kb/' + encodeURIComponent(coLib.id));
    const body = await page.$eval('#page-body', e => e.innerText);
    const vis = async t => page.$$eval('button', (e, t) => e.some(x => x.textContent.trim() === t && x.offsetParent !== null), t);
    ok('9.9 公司的库没有删除、加来源、重新收录', !(await vis('删除这个资料库')) && !(await vis('添加文件夹…')) && !(await vis('重新收录')) && !/去掉/.test(body.slice(0, 600)), body.slice(0, 120).replace(/\s+/g, ' '));
    await page.click('button:has-text("停用")'); await page.waitForTimeout(800);
    const off = ((await sapi('GET', '/api/libraries')).j || []).find(l => l.id === coLib.id);
    ok('9.10 能停用', off && off.enabled === false && /已停用/.test(await txt(page, '.lib-status')), off && off.enabled);
    await page.click('button:has-text("启用")'); await page.waitForTimeout(800);
    const on2 = ((await sapi('GET', '/api/libraries')).j || []).find(l => l.id === coLib.id);
    ok('9.11 能启用', on2 && on2.enabled === true, on2 && on2.enabled);
    await page.click('.doc-open >> nth=0'); await page.waitForTimeout(700);
    const ro = await page.$('.dc-ro'), ti = await page.$('.tag-input'), ed = await page.$('.dc-edit'), rg = await page.$('#doc-card button:has-text("重新写摘要")');
    ok('9.12 公司库的文档卡片里不能改标签和摘要', !!ro && !ti && !ed && !rg, await txt(page, '.dc-ro'));
    await page.screenshot({ path: '/home/claude/shots/r10s_plain_04_company_card.png' });
    await ctx.close(); }
  // 9.5 Hspace 问公司库里有的内容
  { const { page, ctx } = await sopen('#/hspace');
    const r = await askHs(page, '总监级在一线城市出差，住宿每晚不超过多少钱？');
    const hits = []; (r.s.events || []).forEach(e => { if (e.type === 'tool_result' && Array.isArray(e.payload.hits)) e.payload.hits.forEach(h => hits.push(h.library + '·' + h.title)); });
    const ans = (r.s.events || []).filter(e => e.type === 'assistant_message').map(e => e.payload.content).join(' ');
    ok('9.13 回答的出处来自公司的库', hits.length > 0 && hits.every(x => /^公司制度/.test(x)) && /\[\d+\]/.test(ans), { 批准了: r.approved, hits: hits.slice(0, 3), ans: ans.slice(0, 80) });
    await ctx.close(); }
  // 9.6 技能、模板
  { const { page, ctx } = await sopen('#/skills');
    const rows = await page.$$eval('.skill-row', e => e.filter(x => [...x.querySelectorAll('.tag')].some(t => t.textContent === '公司')).map(x => x.innerText.replace(/\s+/g, ' ')));
    ok('9.14 技能页出现两个公司技能，标"公司"', rows.length === 2 && rows.some(t => /contract-check/.test(t)) && rows.some(t => /weekly-report/.test(t)), rows.map(t => t.slice(0, 40)));
    await ctx.close(); }
  { const { page, ctx } = await sopen('#/hspace');
    await page.click('#home-input'); await page.keyboard.type('/'); await page.waitForTimeout(800);
    const sl = await page.$$eval('#slash-menu .slash-item', e => e.map(x => x.innerText.replace(/\s+/g, ' ')));
    ok('9.15 斜杠指令里有公司技能（说明前写"公司"）和公司模板', sl.some(t => /contract-check/.test(t) && /公司/.test(t)) && sl.some(t => /weekly-report/.test(t)) && sl.some(t => /问制度/.test(t)), sl.filter(t => /公司|问制度/.test(t)).map(t => t.slice(0, 50)));
    await page.keyboard.press('Escape'); await page.fill('#home-input', '');
    const chip = await page.$('.tpl-chip:has-text("问制度")');
    ok('9.16 新对话页的建议里有公司模板，标"公司"', !!chip && /公司/.test(await chip.innerText()), chip ? await chip.innerText() : '');
    if (chip) { await chip.click(); await page.waitForTimeout(500); }
    const labels = await page.$$eval('#home-tplform .tf-label', e => e.map(x => x.textContent));
    const libOpts = await page.$$eval('#home-tplform select option', e => e.map(x => x.textContent));
    ok('9.17 带参数的公司模板弹表单（资料库是下拉）', labels.length === 2 && libOpts.some(o => /公司制度/.test(o)), { labels, libOpts });
    await page.selectOption('#home-tplform select', { label: '公司制度' }).catch(() => {});
    await page.fill('#home-tf-1', '总监住宿标准');
    await page.screenshot({ path: '/home/claude/shots/r10s_plain_05_company_tpl.png' });
    await page.press('#home-tf-1', 'Enter'); await page.waitForTimeout(600);
    ok('9.18 公司模板填好进了输入框', /公司制度/.test(await page.$eval('#home-input', e => e.value)), await page.$eval('#home-input', e => e.value));
    await ctx.close(); }
  // 9.7 自己建一个库并导出
  let own = (await sapi('POST', '/api/libraries', { name: '销售部 SOP' })).j;
  await sapi('POST', '/api/libraries/' + own.id + '/sources', { path: '/home/claude/int_docs/公司SOP' });
  for (let i = 0; i < 80; i++) { const L = ((await sapi('GET', '/api/libraries')).j || []).find(x => x.id === own.id); if (L && L.state === 'ready' && L.docs > 0) break; await sleep(500); }
  { const { page, ctx } = await sopen('#/hspace/kb/' + encodeURIComponent(own.id));
    if (!(await page.$('button:has-text("导出给大家用")'))) { await page.screenshot({ path: '/home/claude/shots/r10s_debug_export.png' }); console.log('DEBUG', (await txt(page, '#page-body')).slice(0, 400), JSON.stringify(((await sapi('GET', '/api/libraries')).j || []).map(l => [l.name, l.state, l.docs, l.shared]))); }
    await page.click('button:has-text("导出给大家用")', { timeout: 5000 }).catch(() => {}); await page.waitForTimeout(2000);
    const o = await txt(page, '.lib-export-out');
    const mf = SH + '/libraries/销售部 SOP/manifest.json';
    const exists = fs.existsSync(mf) && fs.statSync(mf).mtimeMs / 1000 >= t9;
    ok('9.19 导出给大家用：显示写到了哪里和版本号，共享目录里真的多了一个文件夹', /导出好了：第 \d+ 版/.test(o) && /shared_e2e\/libraries\/销售部 SOP/.test(o) && exists, o + ' | 文件夹在=' + exists);
    await page.screenshot({ path: '/home/claude/shots/r10s_plain_06_export.png' });
    await ctx.close(); }
  // 9.8 没答上来的问题交给管资料的人
  { const { page, ctx } = await sopen('#/hspace');
    await askHs(page, '公司给员工养宠物有补贴吗？');
    await ctx.close(); }
  { const { page, ctx } = await sopen('#/hspace/kb');
    const n = (await page.$$('.gap')).length;
    await page.click('button:has-text("交给管资料的人")'); await page.waitForTimeout(1000);
    const t = await page.$$eval('.toast-text', e => e.map(x => x.textContent).join(' | '));
    const files = fs.readdirSync(SH + '/inbox/gaps').filter(f => fs.statSync(SH + '/inbox/gaps/' + f).mtimeMs / 1000 >= t9);
    ok('9.20 交给管资料的人：提示里有路径，inbox/gaps/ 下真的有那个 CSV', n >= 1 && /shared_e2e\/inbox\/gaps\//.test(t) && files.length >= 1 && /宠物/.test(fs.readFileSync(SH + '/inbox/gaps/' + files[0], 'utf8')), { 提示: t, 文件: files });
    await ctx.close(); }
  // 9.9 共享目录连不上：同一个路径读不到（把文件夹临时改名，走完改回来）
  fs.renameSync(SH, SH + '_暂时移开');
  try {
    { const { page, ctx } = await sopen('#/settings');
      await page.click('button:text-is("检查")'); await page.waitForTimeout(2500);
      const t = await page.evaluate(() => { const x = [...document.querySelectorAll('.panel-title')].find(y => y.textContent === '公司共享目录'); return x ? x.closest('section').innerText.replace(/\s+/g, ' ') : ''; });
      ok('9.21 共享目录读不到：界面说连不上、用的是上次同步的内容', /连不上/.test(t) && /同步的内容/.test(t) && /2 个技能 · 3 条模板/.test(t), t.slice(0, 260));
      await page.evaluate(() => { const t = [...document.querySelectorAll('.panel-title')].find(x => x.textContent === '公司共享目录'); if (t) t.scrollIntoView(); }); await page.waitForTimeout(200);
      await page.screenshot({ path: '/home/claude/shots/r10s_plain_07_unreachable.png' });
      await ctx.close(); }
    { const libs = (await sapi('GET', '/api/libraries')).j || [], sk = (await sapi('GET', '/api/skills')).j, tp = (await sapi('GET', '/api/templates')).j || [];
      const skl = (Array.isArray(sk) ? sk : sk.skills || []).filter(x => x.source === 'shared');
      ok('9.22 连不上时公司的库、技能、模板都还在', libs.some(l => l.shared) && skl.length === 2 && tp.filter(x => x.source === 'shared').length === 3, { 库: libs.filter(l => l.shared).map(l => l.name), 技能: skl.map(x => x.name), 模板: tp.filter(x => x.source === 'shared').length });
      const { page, ctx } = await sopen('#/hspace/kb');
      ok('9.23 连不上时知识库页照样有公司的库', /公司制度/.test(await txt(page, '.lib-list')), await txt(page, '.lib-list'));
      await page.screenshot({ path: '/home/claude/shots/r10s_plain_08_unreachable_kb.png' });
      await ctx.close(); }
  } finally { fs.renameSync(SH + '_暂时移开', SH); }
  // 9.10 改成另一个不存在的路径（主控原来的写法）：后端的缓存按路径分，换了路径就没有"上次同步的内容"了，这里只记录看到的
  { const { page, ctx } = await sopen('#/settings');
    await page.fill('input[aria-label="共享目录的路径"]', '/home/claude/shared_e2e_不存在');
    await page.click('button:has-text("保存并检查")'); await page.waitForTimeout(2500);
    const t = await page.evaluate(() => { const x = [...document.querySelectorAll('.panel-title')].find(y => y.textContent === '公司共享目录'); return x ? x.closest('section').innerText.replace(/\s+/g, ' ') : ''; });
    const sk = (await sapi('GET', '/api/skills')).j, tp = (await sapi('GET', '/api/templates')).j || [], libs = (await sapi('GET', '/api/libraries')).j || [];
    ok('9.24 换成另一个不存在的路径：界面说连不上（记录：公司技能 / 模板是否还在）', /连不上/.test(t), { 界面: (t.match(/连不上[^上]*/) || [''])[0].slice(0, 80), 公司技能: (Array.isArray(sk) ? sk : sk.skills || []).filter(x => x.source === 'shared').length, 公司模板: tp.filter(x => x.source === 'shared').length, 公司库: libs.filter(l => l.shared).length });
    // 改回来
    await page.fill('input[aria-label="共享目录的路径"]', SH);
    await page.click('button:has-text("保存并检查")'); await page.waitForTimeout(2000);
    await ctx.close(); }
  // 深色再挑一张：公司的库
  await sapi('POST', '/api/settings', { ui: { theme: 'ink-night' } });
  { const { page, ctx } = await sopen('#/hspace/kb/' + encodeURIComponent(coLib.id), { keepTheme: true });
    await page.waitForTimeout(500);
    await page.screenshot({ path: '/home/claude/shots/r10s_dark_02_company_lib.png' });
    await ctx.close(); }
  srv.kill();
  await browser.close();
  out.forEach(l => console.log(l));
  console.log(errs.length ? '页面报错 / 4xx / 5xx：\n  ' + [...new Set(errs)].join('\n  ') : '页面没有报错，没有 4xx / 5xx');
  if (out.some(l => /^FAIL/.test(l))) process.exit(1);
})();
