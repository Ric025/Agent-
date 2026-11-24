// 主控的联调：真实后端（演示模型）+ 无界面浏览器，走一遍斜杠指令
const { chromium } = require('playwright');
const fs = require('fs');
(async () => {
  const home = process.env.CA_HOME, port = process.env.CA_PORT;
  const token = fs.readFileSync(home + '/token', 'utf8').trim();
  const base = 'http://127.0.0.1:' + port;
  const browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox'] });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const errs = [], reqs = [];
  page.on('pageerror', e => errs.push('pageerror: ' + String(e).slice(0, 200)));
  page.on('console', m => { if (m.type() === 'error') errs.push('console: ' + m.text().slice(0, 200)); });
  page.on('request', r => { if (r.method() !== 'GET' && r.url().includes('/api/')) reqs.push(r.method() + ' ' + r.url().replace(base, '') + ' ' + (r.postData() || '').slice(0, 160)); });
  const out = [];
  const ok = (name, cond, extra) => out.push((cond ? 'ok   ' : 'FAIL ') + name + (extra ? '  ' + extra : ''));
  await page.goto(base + '/?t=' + token, { waitUntil: 'networkidle' });
  await page.waitForTimeout(500);
  const inp = '#home-input';
  // 1 打 / 弹菜单
  await page.click(inp); await page.keyboard.type('/');
  await page.waitForTimeout(300);
  let n = await page.$$eval('#slash-menu [role="option"]', els => els.length);
  ok('打 / 弹出菜单', n >= 10, n + ' 项');
  ok('aria-expanded', (await page.getAttribute(inp, 'aria-expanded')) === 'true');
  // 2 筛选
  await page.keyboard.type('记');
  await page.waitForTimeout(200);
  let names = await page.$$eval('#slash-menu [role="option"]', els => els.map(e => e.textContent.replace(/\s+/g, ' ').slice(0, 20)));
  ok('筛选 /记：名字匹配的排最前', names.length >= 2 && /记住/.test(names[0]) && /记忆/.test(names[1]), JSON.stringify(names.slice(0,3)));
  // 3 Esc 关闭，文字留着
  await page.keyboard.press('Escape'); await page.waitForTimeout(150);
  ok('Esc 关闭且保留文字', (await page.inputValue(inp)) === '/记' && !(await page.isVisible('#slash-menu')));
  // 4 /记住 内容
  await page.fill(inp, ''); await page.keyboard.type('/记住 我的助理叫小周'); await page.keyboard.press('Enter');
  await page.waitForTimeout(800);
  ok('/记住 发了记忆请求', reqs.some(r => r.startsWith('POST /api/memories') && r.includes('小周')), reqs.filter(r => r.includes('memories')).join(' | '));
  // 5 全角和顿号开头
  await page.fill(inp, ''); await page.keyboard.type('、xin'); await page.waitForTimeout(200);
  ok('、开头也弹菜单', await page.isVisible('#slash-menu'));
  await page.fill(inp, '');
  // 6 未知指令当普通消息；路径
  await page.keyboard.type('/Users/zhang/报告.docx 这个文件帮我看看'); await page.waitForTimeout(200);
  await page.keyboard.press('Enter'); await page.waitForTimeout(2500);
  ok('路径当普通消息发出', reqs.some(r => r.startsWith('POST /api/sessions ') && r.includes('/Users/zhang')), reqs.filter(r => r.startsWith('POST /api/sessions')).slice(-1)[0]);
  // 现在在对话页
  const ta = await page.evaluate(() => { const a = [...document.querySelectorAll('textarea')].filter(t => t.offsetParent && t.id !== 'home-input'); return a.length ? a[0].id : null; });
  ok('对话页有输入框', !!ta, ta);
  const cin = '#' + ta;
  // 7 /改名
  await page.click(cin); await page.keyboard.type('/改名 看一份报告'); await page.keyboard.press('Enter'); await page.waitForTimeout(700);
  ok('/改名 发了 PATCH', reqs.some(r => r.startsWith('PATCH /api/sessions/') && r.includes('看一份报告')));
  const sideHas = await page.evaluate(() => document.querySelector('#task-list').textContent.includes('看一份报告'));
  ok('侧栏标题更新', sideHas);
  // 8 技能指令
  await page.click(cin); await page.keyboard.type('/sl'); await page.waitForTimeout(250);
  const first = await page.$eval('#slash-menu [role="option"]', e => e.textContent.replace(/\s+/g, ' ').slice(0, 30));
  await page.keyboard.press('Enter'); await page.waitForTimeout(200);
  const filled = await page.inputValue(cin);
  await page.keyboard.type('把季度数据做成 5 页'); await page.keyboard.press('Enter'); await page.waitForTimeout(2500);
  ok('技能指令带 skill 字段', reqs.some(r => /POST \/api\/sessions\/[\w-]+\/message/.test(r) && r.includes('"skill"')), first + ' → 填入「' + filled + '」');
  const chip = await page.evaluate(() => document.body.textContent.includes('技能：'));
  ok('气泡上有技能标签', chip);
  const pill = await page.evaluate(() => document.body.textContent.includes('运行中'));
  ok('做完后不再显示运行中', !pill);
  // 9 /整理
  await page.click(cin); await page.keyboard.type('/整理 保留数字'); await page.keyboard.press('Enter'); await page.waitForTimeout(2500);
  await page.screenshot({ path: '/home/claude/shots/e2e_compact.png' });
  ok('/整理 发了 compact', reqs.some(r => r.includes('/control') && r.includes('compact') && r.includes('保留数字')));
  // 10 /状态
  await page.click(cin); await page.keyboard.type('/状态'); await page.keyboard.press('Enter'); await page.waitForTimeout(900);
  const card = await page.evaluate(() => { const c = document.querySelector('#slash-card'); return c && !c.hidden ? c.textContent.replace(/\s+/g, ' ').slice(0, 200) : null; });
  ok('/状态 卡片', !!card && /上下文/.test(card), card);
  await page.screenshot({ path: '/home/claude/shots/e2e_status.png' });
  await page.keyboard.press('Escape');
  // 11 /主题
  await page.click(cin); await page.keyboard.type('/主题 墨夜'); await page.keyboard.press('Enter'); await page.waitForTimeout(600);
  const th = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));
  ok('/主题 墨夜 生效', th === 'ink-night', th);
  ok('主题存到后端', reqs.some(r => r.startsWith('POST /api/settings') && r.includes('ink-night')));
  // 12 /新对话
  await page.click(cin); await page.keyboard.type('/new'); await page.keyboard.press('Enter'); await page.waitForTimeout(500);
  ok('/new 回到新对话页', await page.isVisible('#home-input'));
  console.log(out.join('\n')); console.log('--- 请求 ---\n' + reqs.join('\n'));
  console.log(errs.length ? 'ERRORS ' + JSON.stringify(errs.slice(0, 6)) : '页面没有报错');
  await browser.close();
})();
