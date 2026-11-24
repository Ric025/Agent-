// 单张截图：node shot_one.js <输出名> <hash> [JS 表达式，在页面里执行] [宽x高]
// 环境变量 CA_PORT、CA_HOME、CA_THEME。用于临时看某个状态（比如设置页底部、打开的菜单）。
const { chromium } = require('playwright');
const fs = require('fs');
(async () => {
  const [name, hash, js, size] = process.argv.slice(2);
  const home = process.env.CA_HOME || '/home/claude/home_d';
  const token = fs.readFileSync(home + '/token', 'utf8').trim();
  let port = process.env.CA_PORT;
  if (!port) { try { port = (/http:\/\/127\.0\.0\.1:(\d+)/.exec(fs.readFileSync(home + '/server.log', 'utf8')) || [])[1]; } catch (e) {} }
  const base = 'http://127.0.0.1:' + (port || '18903');
  const theme = process.env.CA_THEME || 'plain';
  const [w, h] = (size || '1440x900').split('x').map(Number);
  // 后端设置里的 ui.theme 优先于本地（界面启动时以后端为准），所以先把要截的主题存到后端
  try {
    await fetch(base + '/api/settings', { method: 'POST', headers: { 'X-Token': token, 'Content-Type': 'application/json',
      Origin: base }, body: JSON.stringify({ ui: { theme } }) });
  } catch (e) { console.log('存主题到后端失败', String(e)); }
  const browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox'] });
  const ctx = await browser.newContext({ viewport: { width: w, height: h } });
  await ctx.addInitScript(t => { try { if (t === 'auto') localStorage.removeItem('ca.theme'); else localStorage.setItem('ca.theme', t); } catch (e) {} }, theme);
  const page = await ctx.newPage();
  const errs = [];
  page.on('pageerror', e => errs.push(String(e)));
  page.on('console', m => { if (m.type() === 'error') errs.push('console: ' + m.text()); });
  page.on('response', r => { if (r.status() >= 400) errs.push(r.status() + ' ' + r.url().replace(/[?&]t=[^&]+/, '')); });
  await page.goto(base + '/?t=' + token + (hash || ''), { waitUntil: 'networkidle' });
  await page.waitForTimeout(600);
  if (js) { try { await page.evaluate(js); } catch (e) { errs.push('js: ' + e); } await page.waitForTimeout(500); }
  await page.screenshot({ path: '/home/claude/shots/' + name + '.png' });
  console.log(name, errs.length ? errs : 'ok');
  await browser.close();
})();
