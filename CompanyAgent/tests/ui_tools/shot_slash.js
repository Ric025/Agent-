// 斜杠指令的截图。用法：bash /home/claude/shot.sh slash 18901 /home/claude/home_b /home/claude/tools/shot_slash.js
const { chromium } = require('playwright');
const fs = require('fs');
(async () => {
  const token = fs.readFileSync((process.env.CA_HOME || '/home/claude/home_b') + '/token', 'utf8').trim();
  const base = 'http://127.0.0.1:' + (process.env.CA_PORT || '18901');
  const browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox'] });
  const out = '/home/claude/shots/';
  const tag = process.argv[2] || 'slash';
  const only = process.env.ONLY ? process.env.ONLY.split(',') : null;
  async function run(name, opts, fn) {
    if (only && !only.some(o => name.startsWith(o))) return;
    const ctx = await browser.newContext(Object.assign({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1 }, opts));
    const page = await ctx.newPage();
    page.setDefaultTimeout(6000);
    const errs = [];
    const reqs = [];
    page.on('pageerror', e => errs.push(String(e).slice(0, 200)));
    page.on('console', m => { if (m.type() === 'error') errs.push('console: ' + m.text().slice(0, 200)); });
    // 假后端不认 skill：记下请求体，并在读对话时给第一条用户发言补上 skill，看气泡标签
    await page.route('**/api/sessions**', async (route) => {
      const req = route.request();
      if (req.method() !== 'GET') reqs.push(req.method() + ' ' + new URL(req.url()).pathname + ' ' + (req.postData() || ''));
      if (req.method() === 'GET' && /\/api\/sessions\/[\w-]+$/.test(new URL(req.url()).pathname) && process.env.FAKE_SKILL) {
        const r = await route.fetch();
        const d = await r.json();
        (d.events || []).some(e => { if (e.type === 'user_message') { e.payload.skill = 'slides'; return true; } return false; });
        return route.fulfill({ response: r, json: d });
      }
      return route.continue();
    });
    await page.goto(base + '/?t=' + token + (opts.hash || ''), { waitUntil: 'networkidle' });
    await page.waitForTimeout(500);
    try { await fn(page); } catch (e) { errs.push('script: ' + String(e).slice(0, 300)); }
    await page.screenshot({ path: out + tag + '_' + name + '.png' });
    const aria = await page.evaluate(() => {
      const i = document.activeElement;
      const t = document.documentElement.getAttribute('data-theme');
      const toasts = Array.from(document.querySelectorAll('.toast-text')).map(x => x.textContent);
      const banner = document.getElementById('task-banner'); const title = document.getElementById('task-title');
      return { theme: t, toasts, banner: banner && !banner.hidden ? banner.textContent : '', title: title ? title.textContent : '',
        input: i && i.tagName === 'TEXTAREA' ? { exp: i.getAttribute('aria-expanded'), ad: i.getAttribute('aria-activedescendant'), val: i.value } : null };
    });
    console.log(name, errs.length ? 'ERRORS ' + JSON.stringify(errs.slice(0, 5)) : 'ok', JSON.stringify(aria), reqs.length ? JSON.stringify(reqs) : '');
    await ctx.close();
  }
  const firstTask = async p => {
    await p.evaluate(() => { const a = document.querySelector('#task-list a.task-item'); if (a) location.hash = a.getAttribute('href'); });
    await p.waitForTimeout(900);
  };
  await run('01_home_slash', {}, async p => { await p.click('#home-input'); await p.keyboard.type('/'); await p.waitForTimeout(600); });
  await run('02_home_filter', {}, async p => { await p.click('#home-input'); await p.keyboard.type('/ji');await p.keyboard.press('Backspace');await p.keyboard.press('Backspace');await p.keyboard.type('记'); await p.keyboard.press('ArrowDown'); await p.waitForTimeout(300); });
  await run('03_fullwidth', {}, async p => { await p.click('#home-input'); await p.keyboard.type('、记'); await p.waitForTimeout(300); });
  await run('04_unknown', {}, async p => { await p.click('#home-input'); await p.keyboard.type('/Users/zhang/x.xlsx'); await p.waitForTimeout(300); });
  await run('05_arg', {}, async p => {
    await p.click('#home-input'); await p.keyboard.type('/记住'); await p.keyboard.press('Enter'); await p.keyboard.type('测试一条记忆');
    await p.waitForTimeout(300);
  });
  await run('06_remember_done', {}, async p => {
    await p.click('#home-input'); await p.keyboard.type('/记住 测试一条记忆'); await p.keyboard.press('Enter'); await p.waitForTimeout(800);
  });
  await run('07_memory_cmd', {}, async p => { await p.click('#home-input'); await p.keyboard.type('/memory'); await p.keyboard.press('Enter'); await p.waitForTimeout(800); });
  await run('08_theme_pick', {}, async p => { await p.click('#home-input'); await p.keyboard.type('/主题'); await p.keyboard.press('Enter'); await p.waitForTimeout(300); });
  await run('09_status_home', {}, async p => { await p.click('#home-input'); await p.keyboard.type('/状态'); await p.keyboard.press('Enter'); await p.waitForTimeout(800); });
  await run('10_skill_send', {}, async p => {
    await p.click('#home-input'); await p.keyboard.type('/sl'); await p.waitForTimeout(300);
    await p.keyboard.press('Enter'); await p.keyboard.type('把季度数据做成 5 页'); await p.keyboard.press('Enter'); await p.waitForTimeout(2500);
  });
  await run('11_chat_slash', { env: 1 }, async p => {
    await firstTask(p);
    await p.click('#composer-input'); await p.keyboard.type('/'); await p.waitForTimeout(500);
  });
  await run('12_chat_bubble', {}, async p => {
    await firstTask(p);
    await p.click('#composer-input'); await p.keyboard.type('/改名 季度汇报'); await p.waitForTimeout(300);
  });
  await run('13_chat_status', {}, async p => {
    await firstTask(p);
    await p.click('#composer-input'); await p.keyboard.type('/status'); await p.keyboard.press('Enter'); await p.waitForTimeout(800);
  });
  await run('14_help', {}, async p => {
    await firstTask(p);
    await p.click('#composer-input'); await p.keyboard.type('/帮助'); await p.keyboard.press('Enter'); await p.waitForTimeout(500);
  });
  await run('15_narrow', { viewport: { width: 400, height: 780 } }, async p => { await p.click('#home-input'); await p.keyboard.type('/'); await p.waitForTimeout(500); });
  await run('16_narrow_chat', { viewport: { width: 400, height: 780 } }, async p => {
    await firstTask(p);
    await p.click('#composer-input'); await p.keyboard.type('/'); await p.waitForTimeout(500);
  });
  await run('17_plus', {}, async p => {
    await p.click('#home-plus'); await p.waitForTimeout(200);
    await p.click('text=指令…'); await p.waitForTimeout(400);
  });
  await run('18_esc', {}, async p => {
    await p.click('#home-input'); await p.keyboard.type('/new'); await p.keyboard.press('Escape'); await p.waitForTimeout(300);
  });
  await run('19_compact', {}, async p => {
    await firstTask(p);
    await p.click('#composer-input'); await p.keyboard.type('/整理 保留数字'); await p.keyboard.press('Enter'); await p.waitForTimeout(1500);
  });
  await run('20_rename', {}, async p => {
    await firstTask(p);
    await p.click('#composer-input'); await p.keyboard.type('/改名 季度汇报'); await p.keyboard.press('Enter'); await p.waitForTimeout(800);
  });
  await run('21_theme_set', {}, async p => {
    await p.click('#home-input'); await p.keyboard.type('/theme 墨夜'); await p.keyboard.press('Enter'); await p.waitForTimeout(800);
  });
  await run('22_theme_reload', {}, async p => { await p.waitForTimeout(500); });
  await run('23_theme_auto', {}, async p => {
    await p.click('#home-input'); await p.keyboard.type('/主题'); await p.keyboard.press('Enter'); await p.keyboard.press('Enter'); await p.waitForTimeout(800);
  });
  await browser.close();
})();
