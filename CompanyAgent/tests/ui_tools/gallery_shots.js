// 主题画廊截图：node gallery_shots.js [标签前缀，默认 gallery]
// 出 /home/claude/shots/<前缀>_light.png、<前缀>_dark.png（总览）和 <前缀>_<主题id>.png（每个主题一张大图）。
const { chromium } = require('playwright');
const path = require('path');
const { list } = require('/home/claude/ca/ui/theme.js');
(async () => {
  const tag = process.argv[2] || 'gallery';
  const out = '/home/claude/shots/';
  const url = 'file://' + path.join(__dirname, 'gallery.html');
  const browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox', '--allow-file-access-from-files'] });
  const page = await browser.newPage({ viewport: { width: 2800, height: 1700 }, deviceScaleFactor: 1 });
  const errs = [];
  page.on('pageerror', e => errs.push(String(e)));
  page.on('console', m => { if (m.type() === 'error') errs.push(m.text()); });
  for (const g of ['light', 'dark']) {
    await page.goto(url + '?group=' + g);
    await page.waitForTimeout(300);
    await (await page.$('#grid')).screenshot({ path: out + tag + '_' + g + '.png' });
    console.log(tag + '_' + g + '.png');
  }
  await page.setViewportSize({ width: 960, height: 900 });
  for (const t of list) {
    await page.goto(url + '?only=' + t.id);
    await page.waitForTimeout(200);
    const el = await page.$('.sw');
    await el.screenshot({ path: out + tag + '_' + t.id + '.png' });
    console.log(tag + '_' + t.id + '.png');
  }
  await browser.close();
  if (errs.length) { console.log('ERRORS', errs); process.exit(1); }
})();
