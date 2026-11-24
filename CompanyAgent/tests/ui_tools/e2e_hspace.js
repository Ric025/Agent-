// 主控的联调：新后端（演示模型）+ 新界面，走一遍两种模式和 Hspace
const { chromium } = require('playwright');
const fs = require('fs');
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const home = process.env.CA_HOME, port = process.env.CA_PORT, docs = process.env.DOCS;
  const token = fs.readFileSync(home + '/token', 'utf8').trim();
  const base = 'http://127.0.0.1:' + port;
  const H = { 'X-Token': token, 'Content-Type': 'application/json', Origin: base };
  const api = async (m, p, b) => { const r = await fetch(base + p, { method: m, headers: H, body: b ? JSON.stringify(b) : undefined }); let j = null; try { j = await r.json(); } catch (e) {} return { s: r.status, j }; };
  const out = [], errs = [];
  const ok = (n, c, x) => out.push((c ? 'ok   ' : 'FAIL ') + n + (x !== undefined ? '  ' + String(typeof x === 'string' ? x : JSON.stringify(x)).slice(0, 240) : ''));
  // ---- 接口 ----
  const st = (await api('GET', '/api/state')).j;
  ok('state.modes 两个', Array.isArray(st.modes) && st.modes.length === 2 && st.modes[0].id === 'cariai' && st.modes[1].id === 'hspace', st.modes && st.modes.map(m => m.name + '：' + m.tagline));
  let r = await api('POST', '/api/libraries', { name: '公司 SOP', note: '制度、流程' });
  const lib = r.j; ok('新建资料库', r.s === 200 && lib && lib.id, lib);
  r = await api('POST', '/api/libraries/' + lib.id + '/sources', { path: docs });
  ok('加来源并开始收录', r.s === 200, r.j);
  let L = null;
  for (let i = 0; i < 60; i++) { L = (await api('GET', '/api/libraries')).j.find(x => x.id === lib.id); if (L && L.state === 'ready' && L.docs >= 6) break; await sleep(500); }
  ok('收录完成', L && L.state === 'ready', { docs: L.docs, parts: L.parts, failed: L.failed, vectors: L.vectors, synced: !!L.synced });
  const dl = (await api('GET', '/api/libraries/' + lib.id + '/docs')).j;
  ok('文档列表', Array.isArray(dl) && dl.length >= 6, dl.map(d => d.title + '(' + d.kind + ',' + d.parts + (d.version_label ? ',' + d.version_label : '') + ')'));
  r = await api('POST', '/api/kb/search', { query: '总监住宿标准' });
  const hs = r.j.hits || [];
  ok('检索测试：新版排第一、旧版标出', hs.length >= 2 && /2026/.test(hs[0].title) && hs.some(h => h.superseded), hs.slice(0, 3).map(h => h.n + ' ' + h.title + ' · ' + h.loc + (h.superseded ? '（旧版）' : '') + ' via=' + h.via));
  r = await api('POST', '/api/kb/search', { query: '报销多久到账' });
  ok('检索测试：问答对', (r.j.hits || []).some(h => h.kind === 'qa'), (r.j.hits || []).slice(0, 2).map(h => h.kind + ' ' + h.title + ' ' + h.text.slice(0, 30)));
  r = await api('POST', '/api/kb/search', { query: '华东销售额' });
  ok('检索测试：PPT 按页', (r.j.hits || []).some(h => /pptx$/.test(h.title) && /第 \d+ 页/.test(h.loc)), (r.j.hits || []).slice(0, 2).map(h => h.title + ' · ' + h.loc));
  const caps = (await api('GET', '/api/capabilities')).j;
  const sem = (caps.features || []).find(f => f.id === 'semantic_search');
  ok('语义检索：没配向量模型时有原话和改用办法', sem && !sem.ok && !!sem.reason && !!sem.fallback, sem && sem.reason + ' | ' + sem.fallback);
  // ---- 界面 ----
  const browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox'] });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  page.on('pageerror', e => errs.push('pageerror: ' + String(e).slice(0, 200)));
  page.on('console', m => { if (m.type() === 'error') errs.push('console: ' + m.text().slice(0, 200)); });
  const shot = n => page.screenshot({ path: '/home/claude/shots/hs_' + n + '.png' });
  await page.goto(base + '/?t=' + token, { waitUntil: 'networkidle' }); await page.waitForTimeout(600);
  const tabs = await page.$$eval('#mode-switch [role="tab"]', els => els.map(e => e.textContent.trim()));
  ok('总开关显示两个模式', tabs.length === 2 && /^CariAi/.test(tabs[0]) && /^Hspace/.test(tabs[1]), tabs);
  await shot('01_cariai_new');
  await page.click('#mode-switch [role="tab"]:nth-child(2)'); await page.waitForTimeout(700);
  let body = await page.evaluate(() => document.body.innerText);
  ok('切到 Hspace：新对话页', /想查什么/.test(body) && /已收录/.test(body), (body.match(/已收录[^\n]*/) || [''])[0]);
  ok('地址带 hspace', /#\/hspace/.test(page.url()), page.url().replace(base, '').replace(token, '…'));
  await shot('02_hspace_new');
  await page.click('#home-input'); await page.keyboard.type('总监级在一线城市住宿每晚不超过多少钱？'); await page.keyboard.press('Enter');
  await page.waitForTimeout(5000);
  body = await page.evaluate(() => document.body.innerText);
  const cites = await page.$$eval('button.cite', els => els.map(e => e.getAttribute('data-n')));
  ok('回答里有出处标签', cites.length >= 1, { cites, 回答: (body.match(/资料里[^\n]*/) || [''])[0].slice(0, 80) });
  ok('回答只显示一遍', (body.match(/资料里是这么写的/g) || []).length === 1, (body.match(/资料里是这么写的/g) || []).length);
  ok('回答下面有"依据"那行', /依据 \d+ 处资料|没有引用资料/.test(body), (body.match(/依据[^\n]*|这段回答没有引用资料/) || [''])[0]);
  const c1 = await page.$('button.cite'); if (c1) await c1.click(); await page.waitForTimeout(700);
  const src = await page.evaluate(() => { const s = document.querySelector('#rp-sources-sec'); return s && !s.hidden ? s.innerText.replace(/\s+/g, ' ').slice(0, 260) : ''; });
  ok('点出处打开来源面板', /差旅管理办法/.test(src) && /800/.test(src), src);
    await shot('03_hspace_answer');
  // 展开检索步骤
  const st1 = await page.$('text=/做了 \\d+ 步/'); if (st1) await st1.click(); await page.waitForTimeout(400);
  body = await page.evaluate(() => document.body.innerText);
  ok('检索步骤用了 display', /在资料里找/.test(body), (body.match(/在资料里找[^\n]*/) || [''])[0]);
  // ---- 进阶：核对数字、反馈、标准答案 ----
  let hsS = (await api('GET', '/api/sessions?mode=hspace')).j; hsS = Array.isArray(hsS) ? hsS : hsS.sessions || [];
  const sid0 = hsS[0] && hsS[0].id;
  const det = (await api('GET', '/api/sessions/' + sid0)).j || {};
  const evs = det.events || [];
  const gr = evs.filter(e => e.type === 'grounding').map(e => e.payload)[0];
  ok('回答后有核对数字的事件', !!gr && gr.ok === true && gr.cited.length >= 1, gr);
  const up = await page.$('button.fb-btn[data-fk="up"]'); if (up) await up.click(); await page.waitForTimeout(600);
  ok('点"有用"', !!up && await page.$eval('button.fb-btn[data-fk="up"]', e => e.getAttribute('aria-pressed')) === 'true');
  const down = await page.$('button.fb-btn[data-fk="down"]'); if (down) await down.click(); await page.waitForTimeout(500);
  const note = await page.$('textarea.fb-note');
  ok('点"不对"出现填正确答案的框', !!note);
  if (note) { await note.click(); await page.keyboard.type('总监级在一线城市每晚不超过 800 元；超过的部分要分管副总裁书面批准。'); }
  await shot('03b_feedback');
  const std = await page.$('button[data-fk="std"]'); if (std) await std.click(); await page.waitForTimeout(1200);
  const ans = (await api('GET', '/api/kb/answers')).j;
  ok('记成标准答案', Array.isArray(ans) && ans.length === 1 && /副总裁/.test(ans[0].answer) && !!ans[0].source, ans && ans[0] && { question: ans[0].question, source: ans[0].source });
  const fbNow = ((await api('GET', '/api/sessions/' + sid0)).j.events || []).filter(e => e.type === 'feedback').map(e => e.payload.rating);
  ok('反馈写进了账本', fbNow.length >= 2 && fbNow[fbNow.length - 1] === 'down', fbNow);
  r = await api('POST', '/api/kb/search', { query: '总监级在一线城市住宿每晚不超过多少钱？' });
  ok('再搜同样的问题：标准答案排第一', (r.j.hits || []).length >= 1 && r.j.hits[0].standard === true, (r.j.hits || []).slice(0, 2).map(h => (h.standard ? '标准答案 ' : '') + h.title + ' cover=' + h.cover));
  r = await api('POST', '/api/kb/search', { query: '公司对海外子公司的外汇套期保值有什么规定？' });
  ok('检索测试：没有的事 hits 为空，弱命中另列', (r.j.hits || []).length === 0 && Array.isArray(r.j.weak), { hits: (r.j.hits || []).length, weak: (r.j.weak || []).slice(0, 2).map(h => h.title + ' cover=' + h.cover) });
  // 资料里没有的问题
  await page.click('#composer-input'); await page.keyboard.type('公司对海外子公司的外汇套期保值有什么规定？'); await page.keyboard.press('Enter');
  await page.waitForTimeout(4500);
  body = await page.evaluate(() => document.body.innerText);
  ok('资料里没有时明说', /没有找到/.test(body) && !/适用于集团及各子公司/.test(body.split('外汇套期保值').pop()), (body.match(/[^\n]*没有找到[^\n]*/) || ['没有说查不到'])[0].slice(0, 90));
  await shot('04_notfound');
  // 对话按模式分开
  const hsList = await page.$$eval('#task-list a, #task-list [role="link"], #task-list button', els => els.map(e => e.textContent.trim().slice(0, 14)).filter(Boolean));
  await page.click('#mode-switch [role="tab"]:nth-child(1)'); await page.waitForTimeout(700);
  const caList = await page.$$eval('#task-list a, #task-list [role="link"], #task-list button', els => els.map(e => e.textContent.trim().slice(0, 14)).filter(Boolean));
  const selNow = await page.$$eval('#mode-switch [role="tab"]', els => els.map(e => e.getAttribute('aria-selected')));
  ok('从 Hspace 的对话里切回 CariAi', selNow[0] === 'true' && !/hspace/.test(page.url()), selNow);
  ok('对话列表按模式分开', hsList.length >= 1 && !caList.some(t => /总监级/.test(t)), { hspace: hsList, cariai: caList });
  await page.click('#home-input'); await page.keyboard.type('看一下这个文件夹里有什么'); await page.keyboard.press('Enter'); await page.waitForTimeout(3500);
  body = await page.evaluate(() => document.body.innerText);
  ok('CariAi 照旧', /演示模式/.test(body) && !/依据 \d+ 处资料|没有引用资料/.test(body));
  // 知识库页
  await page.goto(base + '/?t=' + token + '#/hspace/kb', { waitUntil: 'networkidle' }); await page.waitForTimeout(900);
  body = await page.evaluate(() => document.body.innerText);
  ok('知识库页列出资料库', /公司 SOP/.test(body) && /检索测试/.test(body), (body.match(/公司 SOP[^\n]*/) || [''])[0]);
  await shot('05_kb');
  const libLink = await page.$('text=公司 SOP'); if (libLink) await libLink.click(); await page.waitForTimeout(900);
  body = await page.evaluate(() => document.body.innerText);
  ok('资料库详情有文档表', /差旅管理办法（2026 版）/.test(body) && /预算表_v1/.test(body));
  await shot('06_lib');
  // 设置
  await page.goto(base + '/?t=' + token + '#/hspace/settings', { waitUntil: 'networkidle' }); await page.waitForTimeout(800);
  const adv = await page.$('summary:has-text("高级"), button:has-text("高级")'); if (adv) await adv.click(); await page.waitForTimeout(600);
  body = await page.evaluate(() => document.body.innerText);
  ok('设置页有"语义检索"', /语义检索/.test(body) && /向量模型/.test(body));
  const sessions = (await api('GET', '/api/sessions?mode=hspace')).j;
  const sid = (Array.isArray(sessions) ? sessions : sessions.sessions || [])[0];
  if (sid) { const stt = (await api('GET', '/api/sessions/' + sid.id + '/status')).j; ok('状态里有模式', stt.mode === 'hspace', stt.mode); }
  // ---- 深入研究、标准答案管理 ----
  await page.goto(base + '/?t=' + token + '#/hspace', { waitUntil: 'networkidle' }); await page.waitForTimeout(800);
  const deepBtn = await page.$('#home-deep');
  const deepVisible = deepBtn && await deepBtn.evaluate(e => !e.hidden);
  ok('Hspace 新对话页有"深入研究"开关', !!deepVisible);
  if (deepVisible) { await deepBtn.click(); await page.waitForTimeout(200); }
  await page.click('#home-input'); await page.keyboard.type('总监级在一线城市住宿每晚不超过多少钱？'); await page.keyboard.press('Enter');
  await page.waitForTimeout(6000);
  body = await page.evaluate(() => document.body.innerText);
  let hs2 = (await api('GET', '/api/sessions?mode=hspace')).j; hs2 = Array.isArray(hs2) ? hs2 : hs2.sessions || [];
  const newest = hs2.slice().sort((a, b) => String(b.updated || b.created || '').localeCompare(String(a.updated || a.created || '')))[0];
  const ev2 = ((await api('GET', '/api/sessions/' + newest.id)).j.events || []);
  const um = ev2.filter(e => e.type === 'user_message').map(e => e.payload)[0] || {};
  ok('深入研究：发言带了 depth', um.depth === 'deep', { depth: um.depth });
  ok('深入研究：界面上有标记', /深入研究/.test(body));
  ok('深入研究那轮回答也只显示一遍', (body.match(/资料里是这么写的/g) || []).length === 1);
  ok('新对话里按标准答案回答', /副总裁/.test(body) && /标准答案/.test(body), (body.match(/[^\n]*(副总裁|标准答案)[^\n]*/) || [''])[0].slice(0, 120));
  await shot('07_deep_standard');
  await page.goto(base + '/?t=' + token + '#/hspace/kb', { waitUntil: 'networkidle' }); await page.waitForTimeout(1000);
  body = await page.evaluate(() => document.body.innerText);
  ok('内置的标准答案库不在资料库列表里', !/还没有来源文件夹/.test(body));
  ok('知识库页有标准答案一栏', /标准答案/.test(body) && /副总裁|总监级/.test(body), (body.match(/标准答案[^\n]*/) || [''])[0].slice(0, 80));
  await shot('08_kb_answers');
  console.log(out.join('\n'));
  console.log(errs.length ? 'ERRORS ' + JSON.stringify(errs.slice(0, 6)) : '页面没有报错');
  await browser.close();
})();
