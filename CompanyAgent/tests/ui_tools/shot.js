const { chromium } = require('playwright');
const fs = require('fs');
(async () => {
  const token = fs.readFileSync((process.env.CA_HOME || '/home/claude/ca_home') + '/token', 'utf8').trim();
  const base = 'http://127.0.0.1:' + (process.env.CA_PORT || '18900');
  const browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox'] });
  const out = '/home/claude/shots/';
  const tag = process.argv[2] || 'v6';
  async function run(name, opts, fn) {
    const ctx = await browser.newContext(Object.assign({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 }, opts));
    const page = await ctx.newPage();
    const errs = [];
    page.on('pageerror', e => errs.push(String(e).slice(0, 200)));
    page.on('console', m => { if (m.type() === 'error') errs.push('console: ' + m.text().slice(0, 200)); });
    await page.goto(base + '/?t=' + token, { waitUntil: 'networkidle' });
    await page.waitForTimeout(600);
    try { await fn(page); } catch (e) { errs.push('script: ' + String(e).slice(0, 300)); }
    await page.screenshot({ path: out + tag + '_' + name + '.png' });
    if (errs.length) console.log(name, 'ERRORS', errs.slice(0, 5));
    else console.log(name, 'ok');
    await ctx.close();
  }
  await run('01_home', {}, async p => {});
  await run('02_chat', {}, async p => {
    await p.fill('#home-input', '看一下这个文件夹里有什么，然后告诉我');
    await p.keyboard.press('Enter');
    await p.waitForTimeout(3500);
  });
  await run('03_chat_open', {}, async p => {
    // 打开刚才那个对话并展开步骤
    const first = await p.$('#task-list a, #task-list button, #task-list [role="link"]');
    if (first) await first.click();
    await p.waitForTimeout(800);
    const fold = await p.$('.fold-line, .steps-line, [data-fold], .fold button, .fold');
    if (fold) await fold.click();
    await p.waitForTimeout(400);
  });
  await run('04_memory', {}, async p => { await p.goto(base + '/?t=' + token + '#/memory'); await p.waitForTimeout(800); });
  await run('05_skills', {}, async p => { await p.goto(base + '/?t=' + token + '#/skills'); await p.waitForTimeout(800); });
  await run('06_settings', {}, async p => { await p.goto(base + '/?t=' + token + '#/settings'); await p.waitForTimeout(800); });
  await run('07_home_dark', { colorScheme: 'dark' }, async p => {});
  await run('08_chat_dark', { colorScheme: 'dark' }, async p => {
    const first = await p.$('#task-list a, #task-list button, #task-list [role="link"]');
    if (first) await first.click();
    await p.waitForTimeout(800);
  });
  await run('09_narrow', { viewport: { width: 420, height: 820 } }, async p => {});
  await browser.close();
})();
