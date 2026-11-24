// 整个应用的截图：node shots.js <标签> [主题 id，默认 auto]
// 一般不直接跑，用 shots.sh（它会先写演示数据、起服务、截完关服务）。
// 环境变量：CA_PORT、CA_HOME（读 token）、CA_DEMO_MAIN / CA_DEMO_QUESTION（演示对话的编号，shots.sh 会给）。
// 图存到 /home/claude/shots/<标签>_<主题>_<序号>_<页面>.png。
//
// 界面重做后如果选择器变了，改下面 SEL 里的几项就行（每项是逗号分隔的候选，找到第一个就用）。
const { chromium } = require('playwright');
const fs = require('fs');

const SEL = {
  // 对话里的"做了 N 步"那一行（点它展开步骤）
  stepToggle: '.steps-line[aria-expanded="false"]',
  // 侧栏的对话列表里的一项
  sideItem: '#task-list a.task-item'
};

(async () => {
  const tag = process.argv[2] || 'app';
  const theme = process.argv[3] || 'auto';
  const home = process.env.CA_HOME || '/home/claude/home_c';
  const token = fs.readFileSync(home + '/token', 'utf8').trim();
  const base = 'http://127.0.0.1:' + (process.env.CA_PORT || '18902');
  const main = process.env.CA_DEMO_MAIN || '';
  const question = process.env.CA_DEMO_QUESTION || '';
  const out = '/home/claude/shots/';
  const browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox'] });
  let failed = 0;

  // 找到对话正文所在的那个能滚动的元素，滚到 frac（0 顶部、0.5 中段、1 底部）
  async function scrollChat(p, frac) {
    await p.evaluate(f => {
      let best = null;
      document.querySelectorAll('main *, #app *, body *').forEach(el => {
        const cs = getComputedStyle(el);
        if (!/(auto|scroll)/.test(cs.overflowY) || el.scrollHeight <= el.clientHeight + 4) return;
        if (!best || el.clientHeight * el.clientWidth > best.clientHeight * best.clientWidth) best = el;
      });
      const el = best || document.scrollingElement;
      el.scrollTop = Math.round((el.scrollHeight - el.clientHeight) * f);
    }, frac);
    await p.waitForTimeout(250);
  }
  async function expandSteps(p) {
    const els = await p.$$(SEL.stepToggle);
    for (const el of els) { try { if (await el.isVisible()) await el.click(); } catch (e) { /* 跳过 */ } }
    await p.waitForTimeout(300);
    return els.length;
  }

  // 后端设置里的 ui.theme 优先于本地（界面启动时以后端为准），所以先把要截的主题存到后端
  try {
    await fetch(base + '/api/settings', { method: 'POST', headers: { 'X-Token': token, 'Content-Type': 'application/json',
      Origin: base }, body: JSON.stringify({ ui: { theme } }) });
  } catch (e) { console.log('存主题到后端失败', String(e)); }
  const only = (process.env.CA_ONLY || '').split(',').filter(Boolean);
  async function run(name, opts, hash, fn) {
    if (only.length && !only.some(o => name.startsWith(o))) return;
    const ctx = await browser.newContext(Object.assign({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 }, opts));
    await ctx.addInitScript(t => {
      try { if (t === 'auto') localStorage.removeItem('ca.theme'); else localStorage.setItem('ca.theme', t); } catch (e) {}
    }, theme);
    const page = await ctx.newPage();
    const errs = [];
    page.on('pageerror', e => errs.push(String(e).slice(0, 200)));
    page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errs.push('console: ' + m.text().slice(0, 200)); });
    page.on('response', r => { if (r.status() >= 400) errs.push(r.status() + ' ' + r.url().replace(/[?&]t=[^&]+/, '')); });
    await page.goto(base + '/?t=' + token + (hash || ''), { waitUntil: 'networkidle' });
    await page.waitForTimeout(700);
    try { if (fn) await fn(page); } catch (e) { errs.push('script: ' + String(e).slice(0, 300)); }
    const file = `${tag}_${theme}_${name}.png`;
    await page.screenshot({ path: out + file });
    if (errs.length) { failed++; console.log(file, 'ERRORS', errs.slice(0, 5)); }
    else console.log(file, 'ok');
    await ctx.close();
  }

  const task = id => (id ? '#/task/' + encodeURIComponent(id) : '');
  await run('01_new', {}, '#/new');
  await run('02_demo_top', {}, task(main), async p => { await scrollChat(p, 0); });
  await run('03_demo_mid', {}, task(main), async p => { await scrollChat(p, 0.5); });
  await run('04_demo_bottom_steps', {}, task(main), async p => {
    const n = await expandSteps(p);
    if (!n) console.log('  （没找到"做了 N 步"，改 shots.js 里的 SEL.stepToggle）');
    await scrollChat(p, 1);
  });
  await run('04b_demo_top_steps', {}, task(main), async p => { await expandSteps(p); await scrollChat(p, 0); });
  await run('05_question', {}, task(question), async p => { await scrollChat(p, 1); });
  await run('06_memory', {}, '#/memory');
  await run('07_skills', {}, '#/skills');
  await run('08_schedule', {}, '#/schedule');
  await run('09_kb', {}, '#/kb');
  await run('10_settings', {}, '#/settings');
  await run('11_narrow_new', { viewport: { width: 420, height: 820 } }, '#/new');
  await run('12_narrow_demo', { viewport: { width: 420, height: 820 } }, task(main), async p => { await scrollChat(p, 1); });
  await browser.close();
  if (failed) process.exit(1);
})();
