// 主题的测试：node tests/theme_test.mjs
// 覆盖：ui/themes.css 里十个主题都在、变量一个不缺、和 ui/theme.js 的清单对得上、
// 没有 data-theme 时的兜底和素白 / 墨夜一致、每个主题几组关键颜色的对比度（WCAG 公式，半透明色先合成到底色上）。
import { createRequire } from 'module';
import { fileURLToPath } from 'url';
import path from 'path';
import fs from 'fs';
import assert from 'assert/strict';

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const uiDir = path.join(here, '..', 'ui');
const { list } = require(path.join(uiDir, 'theme.js'));
const cssText = fs.readFileSync(path.join(uiDir, 'themes.css'), 'utf8');

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log('  ok  ' + name); }
  catch (e) { failed++; console.log('  FAIL ' + name + '\n       ' + (e && e.message ? e.message.split('\n').join('\n       ') : e)); }
}

// 规格 3.1 列的语义变量，加上我们额外约定的（themes.css 开头的注释里有用途）
const REQUIRED = ['--bg', '--bg-side', '--surface', '--surface-2', '--border', '--border-strong', '--text', '--text-2', '--text-3',
  '--accent', '--on-accent', '--accent-soft', '--link', '--ok', '--warn', '--err', '--code-bg', '--focus', '--shadow', '--selection'];
const EXTRA = ['--accent-hover', '--on-accent-soft', '--ok-soft', '--warn-soft', '--err-soft', '--shadow-pop', '--overlay',
  '--rule', '--rule-width', '--font-display'];
const ALL = REQUIRED.concat(EXTRA);
const STATIC = ['--font-sans', '--font-mono', '--font-serif', '--fs-12', '--fs-13', '--fs-14', '--fs-15', '--fs-17', '--fs-20', '--fs-26',
  '--lh-body', '--r-sm', '--r-md', '--r-lg', '--r-xl', '--sp-1', '--sp-4', '--side-w', '--col-w'];

/* ---------- 解析 themes.css：每个最内层的 { } 块 → { selectors, inMedia, decls } ---------- */
function parse(css) {
  css = css.replace(/\/\*[\s\S]*?\*\//g, '');
  const blocks = [];
  const mediaRanges = [];
  const mre = /@media\s*\(prefers-color-scheme:\s*dark\)\s*\{/g;
  let m;
  while ((m = mre.exec(css))) {
    let depth = 1, i = mre.lastIndex;
    for (; i < css.length && depth; i++) { if (css[i] === '{') depth++; else if (css[i] === '}') depth--; }
    mediaRanges.push([m.index, i]);
  }
  const bre = /([^{}]+)\{([^{}]*)\}/g;
  while ((m = bre.exec(css))) {
    const sel = m[1].trim();
    if (sel.startsWith('@')) continue;
    const decls = {};
    m[2].split(';').forEach(d => {
      const k = d.indexOf(':');
      if (k < 0) return;
      const name = d.slice(0, k).trim(), val = d.slice(k + 1).trim();
      if (name) decls[name] = val;
    });
    const inMedia = mediaRanges.some(([a, b]) => m.index > a && m.index < b);
    blocks.push({ selectors: sel.split(',').map(s => s.trim()), inMedia, decls });
  }
  return blocks;
}
const blocks = parse(cssText);
const themes = {};
for (const b of blocks) {
  for (const s of b.selectors) {
    const t = /^\[data-theme="([a-z0-9-]+)"\]$/.exec(s);
    if (t && !b.inMedia) themes[t[1]] = Object.assign(themes[t[1]] || {}, b.decls);
  }
}
const fallbackLight = blocks.find(b => !b.inMedia && b.selectors.includes(':root:not([data-theme])'));
const fallbackDark = blocks.find(b => b.inMedia && b.selectors.includes(':root:not([data-theme])'));
const staticRoot = blocks.find(b => !b.inMedia && b.selectors.length === 1 && b.selectors[0] === ':root');

/* ---------- 颜色 ---------- */
function parseColor(v) {
  v = String(v || '').trim();
  let m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(v);
  if (m) {
    let h = m[1];
    if (h.length === 3) h = h.split('').map(c => c + c).join('');
    return { r: parseInt(h.slice(0, 2), 16), g: parseInt(h.slice(2, 4), 16), b: parseInt(h.slice(4, 6), 16), a: 1 };
  }
  m = /^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)$/i.exec(v);
  if (m) return { r: +m[1], g: +m[2], b: +m[3], a: m[4] == null ? 1 : +m[4] };
  return null;
}
function over(fg, bg) {   // 半透明色合成到不透明的底上
  if (fg.a >= 1) return fg;
  return { r: fg.r * fg.a + bg.r * (1 - fg.a), g: fg.g * fg.a + bg.g * (1 - fg.a), b: fg.b * fg.a + bg.b * (1 - fg.a), a: 1 };
}
function lum(c) {
  const f = x => { x /= 255; return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); };
  return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
}
function ratio(a, b) {
  const x = lum(a), y = lum(b);
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}

// [前景, 底, 最低对比度, 说明]。底若是半透明，先合成到 --bg 上（用户气泡、浅底都画在主区上）
const PAIRS = [];
for (const fg of ['--text', '--text-2']) for (const bg of ['--bg', '--bg-side', '--surface']) PAIRS.push([fg, bg, 4.5, '正文 / 次要文字']);
for (const bg of ['--bg', '--bg-side', '--surface']) PAIRS.push(['--text-3', bg, 3, '弱文字、占位符']);
PAIRS.push(['--text', '--surface-2', 4.5, '正文在悬停 / 当前项的底上']);
PAIRS.push(['--text', '--surface-2@side', 4.5, '侧栏当前项']);
PAIRS.push(['--text', '--code-bg', 4.5, '代码']);
PAIRS.push(['--on-accent', '--accent', 4.5, '强调色按钮上的字']);
PAIRS.push(['--link', '--bg', 4.5, '链接']);
PAIRS.push(['--on-accent-soft', '--accent-soft', 4.5, '用户气泡的字']);
for (const s of ['--ok', '--warn', '--err']) PAIRS.push([s, '--bg', 3, '状态色当文字']);
for (const s of ['--ok', '--warn', '--err']) PAIRS.push(['--text', s + '-soft', 4.5, '状态浅底上的正文']);
PAIRS.push(['--focus', '--bg', 3, '焦点环']);
PAIRS.push(['--border-strong', '--bg', 1.3, '看得见的边']);
PAIRS.push(['--surface-2', '--bg', 1.06, '悬停 / 表头的底和主区分得开']);
PAIRS.push(['--surface-2@side', '--bg-side', 1.06, '侧栏当前项和侧栏分得开']);

function colorOf(theme, name, base) {
  if (name.endsWith('@side')) return colorOf(theme, name.slice(0, -5), colorOf(theme, '--bg-side'));
  const c = parseColor(theme[name]);
  if (!c) throw new Error(`${name} 不是能解析的颜色：${theme[name]}`);
  return base ? over(c, base) : c;
}

/* ---------- 测试 ---------- */
test('theme.js 的清单：十个、先我们的四个再开源的六个、id 合法且不重复', () => {
  assert.equal(list.length, 10);
  assert.deepEqual(list.map(t => t.id), ['plain', 'gongwen', 'celadon', 'ink-night', 'solarized-light', 'catppuccin-latte',
    'nord', 'tokyo-night', 'catppuccin-mocha', 'gruvbox-dark']);
  assert.equal(new Set(list.map(t => t.id)).size, 10);
  for (const t of list) {
    assert.match(t.id, /^[a-z0-9-]{1,32}$/);
    assert.ok(t.name && typeof t.name === 'string');
    assert.equal(typeof t.dark, 'boolean');
  }
  assert.equal(list.filter(t => t.dark).length, 5, '五个深色');
});

test('themes.css 里的主题和 theme.js 的清单一一对应', () => {
  assert.deepEqual(Object.keys(themes).sort(), list.map(t => t.id).sort());
});

test('主题选择器不限定在 :root 上（卡片可以在自己身上写 data-theme）', () => {
  assert.ok(!/:root\s*\[data-theme/.test(cssText.replace(/\/\*[\s\S]*?\*\//g, '')), '出现了 :root[data-theme=…]');
});

for (const t of list) {
  test(`${t.id}（${t.name}）：变量齐全、color-scheme 对`, () => {
    const th = themes[t.id];
    assert.ok(th, '没有这个主题');
    const missing = ALL.filter(v => !(v in th) || th[v] === '');
    assert.deepEqual(missing, [], '缺变量');
    assert.equal(th['color-scheme'], t.dark ? 'dark' : 'light');
    for (const v of ['--bg', '--bg-side', '--surface', '--accent', '--text', '--code-bg']) {
      const c = parseColor(th[v]);
      assert.ok(c && c.a === 1, `${v} 要是不透明的颜色：${th[v]}`);
    }
  });
}

test('文件开头的 :root 定义了字体、字号、圆角、间距', () => {
  assert.ok(staticRoot, '没有单独的 :root 块');
  const missing = STATIC.filter(v => !(v in staticRoot.decls));
  assert.deepEqual(missing, []);
  assert.match(staticRoot.decls['--font-sans'], /PingFang SC/);
  assert.match(staticRoot.decls['--font-sans'], /Microsoft YaHei/);
});

test('公文用宋体当标题字、有 2px 朱红线；其余主题标题用 --font-sans、没有线', () => {
  assert.equal(themes.gongwen['--font-display'], 'var(--font-serif)');
  assert.equal(themes.gongwen['--rule-width'], '2px');
  for (const t of list) {
    if (t.id === 'gongwen') continue;
    assert.equal(themes[t.id]['--font-display'], 'var(--font-sans)', t.id);
    assert.equal(themes[t.id]['--rule-width'], '0px', t.id);
  }
});

test('没有 data-theme 时：浅色和素白一样、深色和墨夜一样', () => {
  assert.ok(fallbackLight && fallbackLight.selectors.includes('[data-theme="plain"]'), '浅色兜底要和素白写在同一块');
  assert.ok(fallbackDark, '缺 @media (prefers-color-scheme: dark) 兜底');
  const ink = themes['ink-night'];
  const diff = Object.keys(ink).filter(k => ink[k] !== fallbackDark.decls[k])
    .concat(Object.keys(fallbackDark.decls).filter(k => !(k in ink)));
  assert.deepEqual(diff, [], '深色兜底和墨夜不一致');
});

let checked = 0;
for (const t of list) {
  test(`${t.id}（${t.name}）：对比度`, () => {
    const th = themes[t.id];
    const bg = colorOf(th, '--bg');
    const bad = [];
    for (const [fg, under, min, what] of PAIRS) {
      const base = colorOf(th, under, bg);   // 半透明的底合成到 --bg 上（@side 的合成到 --bg-side 上）
      const r = ratio(fg.endsWith('@side') ? colorOf(th, fg) : colorOf(th, fg, base), base);
      checked++;
      if (r + 1e-9 < min) bad.push(`${fg} 对 ${under} 只有 ${r.toFixed(2)}，要 ≥ ${min}（${what}）`);
    }
    assert.deepEqual(bad, []);
  });
}

console.log(`\n（共算了 ${checked} 组对比度）`);
console.log(`\n${passed} 通过，${failed} 失败`);
if (failed) process.exit(1);
