// 界面纯函数的测试：node tests/ui_test.mjs
// 覆盖：HTML 转义、Markdown 渲染的安全性（<script>、<img onerror>、javascript: 链接等都不能生效）、
// 事件归并、任务列表更新、工具动作名、记忆 / 搜索 / 技能的数据整理。
import { createRequire } from 'module';
import { fileURLToPath } from 'url';
import path from 'path';
import assert from 'assert/strict';

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const ui = require(path.join(here, '..', 'ui', 'app.js'));

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log('  ok  ' + name); }
  catch (e) { failed++; console.log('  FAIL ' + name + '\n       ' + (e && e.message ? e.message.split('\n').join('\n       ') : e)); }
}

// 输出里允许出现的标签和属性。其余一律算不安全。
const TAGS = new Set(['p', 'br', 'strong', 'em', 'del', 'code', 'pre', 'a', 'ul', 'ol', 'li', 'blockquote', 'hr',
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'div', 'span']);
function assertSafe(html, label) {
  const re = /<\/?([a-zA-Z][a-zA-Z0-9]*)([^>]*)>/g;
  let m;
  while ((m = re.exec(html))) {
    const tag = m[1].toLowerCase();
    assert.ok(TAGS.has(tag), `${label}: 出现了不允许的标签 <${tag}>：${html}`);
    const attrs = m[2];
    // 去掉合法的 名字="值" 之后不能剩下任何东西
    const rest = attrs.replace(/\s+(href|target|rel|class|start)="[^"<>]*"/g, '').trim();
    assert.equal(rest, '', `${label}: 标签上有多余的属性：${attrs}`);
    const href = /href="([^"]*)"/.exec(attrs);
    if (href) {
      const raw = ui.unescapeHtml(href[1]);
      assert.match(raw, /^https?:\/\//i, `${label}: 链接不是 http/https：${raw}`);
    }
  }
  assert.ok(!/<(script|img|iframe|svg|style|object|embed|link|meta|form|input|details)/i.test(html), `${label}: 出现危险标签：${html}`);
}

console.log('转义');

test('escapeHtml 转义五个特殊字符', () => {
  assert.equal(ui.escapeHtml(`<a href="x" onclick='y'>&</a>`), '&lt;a href=&quot;x&quot; onclick=&#39;y&#39;&gt;&amp;&lt;/a&gt;');
  assert.equal(ui.escapeHtml(null), '');
  assert.equal(ui.escapeHtml(12), '12');
});

test('safeUrl 只放行 http/https', () => {
  assert.equal(ui.safeUrl('https://a.com/x?y=1'), 'https://a.com/x?y=1');
  assert.equal(ui.safeUrl('http://10.0.0.1:8000/v1'), 'http://10.0.0.1:8000/v1');
  for (const bad of ['javascript:alert(1)', 'JaVaScRiPt:alert(1)', ' javascript:alert(1)', 'java\tscript:alert(1)',
    'data:text/html,<script>alert(1)</script>', 'vbscript:x', '//evil.com', '/api/file', 'https://a.com/"onmouseover="x',
    "https://a.com/'x", 'https://a.com/<x>', 'https:///nohost', 'file:///etc/passwd', '', null, 42]) {
    assert.equal(ui.safeUrl(bad), null, '应拒绝：' + bad);
  }
});

console.log('Markdown 安全');

const nasty = [
  '<script>alert(1)</script>',
  '<img src=x onerror=alert(1)>',
  '<IMG SRC=x OnError="alert(1)">',
  '<svg onload=alert(1)>',
  '<iframe src="javascript:alert(1)"></iframe>',
  '[点我](javascript:alert(1))',
  '[点我](JAVASCRIPT:alert(1))',
  '[点我](javascript&#58;alert(1))',
  '[点我](data:text/html;base64,PHNjcmlwdD4=)',
  '[x](https://a.com/"onmouseover="alert(1))',
  '[x](https://a.com/ "title" onmouseover="alert(1)")',
  '[<img src=x onerror=alert(1)>](https://a.com)',
  '**<b onclick=alert(1)>粗</b>**',
  '`<script>alert(1)</script>`',
  '```\n<script>alert(1)</script>\n```',
  '```html"><script>alert(1)</script>\nx\n```',
  '| a | <img src=x onerror=1> |\n|---|---|\n| <script>x</script> | [y](javascript:1) |',
  '- <img src=x onerror=1>\n  - [z](javascript:void(0))',
  '> <script>alert(1)</script>\n> > <img onerror=1 src=x>',
  '# <script>alert(1)</script>',
  'https://a.com/<script>alert(1)</script>',
  'http://a.com/"><img src=x onerror=alert(1)>',
  '\u0000<script>\u0000alert(1)</script>',
  '\u00000\u0000<script>',
  '<think>隐藏</think><script>alert(1)</script>',
  '*<script>*alert(1)*</script>*',
  '[a](https://ok.com)<a href="javascript:alert(1)">b</a>',
  '<a href=javascript:alert(1)>x</a>',
  '<style>body{display:none}</style>',
  '&lt;script&gt;alert(1)&lt;/script&gt;',
  '1. [x](https://a.com)\n2. <details open ontoggle=alert(1)>',
  '[**x**](https://a.com/?q="><script>)',
  '[`<img>`](https://a.com)',
];

for (const s of nasty) {
  test('不安全输入被中和：' + JSON.stringify(s).slice(0, 60), () => {
    assertSafe(ui.renderMarkdown(s), JSON.stringify(s));
    assertSafe(ui.renderInline(s), JSON.stringify(s));
  });
}

test('<script> 原样显示成文字', () => {
  const out = ui.renderMarkdown('<script>alert(1)</script>');
  assert.ok(out.includes('&lt;script&gt;alert(1)&lt;/script&gt;'), out);
});

test('<img onerror> 原样显示成文字', () => {
  const out = ui.renderMarkdown('看这里 <img src=x onerror=alert(1)>');
  assert.ok(out.includes('&lt;img src=x onerror=alert(1)&gt;'), out);
  assert.ok(!out.includes('<img'), out);
});

test('javascript: 链接不生成 <a>', () => {
  const out = ui.renderMarkdown('[点我](javascript:alert(1))');
  assert.ok(!out.includes('<a'), out);
  assert.ok(!/href/.test(out), out);
  assert.ok(out.includes('点我'), out);
});

test('正常链接带 noopener，& 被转义', () => {
  const out = ui.renderMarkdown('[报告](https://a.com/x?a=1&b=2)');
  assert.ok(out.includes('<a href="https://a.com/x?a=1&amp;b=2" target="_blank" rel="noopener noreferrer">报告</a>'), out);
});

test('裸网址自动成链接，句末标点不算进去', () => {
  const out = ui.renderInline('见 https://a.com/doc。另见 http://b.com/x.');
  assert.ok(out.includes('<a href="https://a.com/doc"'), out);
  assert.ok(out.includes('<a href="http://b.com/x"'), out);
  assert.ok(out.includes('</a>.'), out);
});

console.log('Markdown 渲染');

test('标题、加粗、斜体、行内代码', () => {
  const out = ui.renderMarkdown('## 结论\n收入 **增长 12%**，*环比*，见 `报告/Q3.md`');
  assert.ok(out.includes('<h2>结论</h2>'), out);
  assert.ok(out.includes('<strong>增长 12%</strong>'), out);
  assert.ok(out.includes('<em>环比</em>'), out);
  assert.ok(out.includes('<code>报告/Q3.md</code>'), out);
});

test('行内代码里的星号不变成加粗', () => {
  assert.equal(ui.renderInline('`a**b**c`'), '<code>a**b**c</code>');
});

test('代码块保留原文', () => {
  const out = ui.renderMarkdown('```python\nif a < b and c > d:\n    print("**x**")\n```\n后面');
  assert.ok(out.includes('<pre class="md-code"><code>if a &lt; b and c &gt; d:\n    print(&quot;**x**&quot;)</code></pre>'), out);
  assert.ok(out.includes('<p>后面</p>'), out);
});

test('没有闭合的代码块不吞掉页面', () => {
  assert.ok(ui.renderMarkdown('```\nabc').includes('<pre class="md-code"><code>abc</code></pre>'));
});

test('无序、有序、嵌套列表', () => {
  const out = ui.renderMarkdown('- 一\n- 二\n  - 二点一\n- 三\n\n1. 甲\n2. 乙');
  assert.ok(out.includes('<ul><li>一</li><li>二<ul><li>二点一</li></ul></li><li>三</li></ul>'), out);
  assert.ok(out.includes('<ol><li>甲</li><li>乙</li></ol>'), out);
});

test('有序列表从 3 开始', () => {
  assert.ok(ui.renderMarkdown('3. 丙\n4. 丁').includes('<ol start="3">'));
});

test('表格：表头、对齐、单元格里的竖线转义', () => {
  const out = ui.renderMarkdown('| 项目 | 金额 |\n|:---|---:|\n| A\\|B | 1,200.00 |\n| C | **3** |');
  assert.ok(out.includes('<table>'), out);
  assert.ok(out.includes('<th class="al-right">金额</th>'), out);
  assert.ok(out.includes('<td>A|B</td>'), out);
  assert.ok(out.includes('<td class="al-right"><strong>3</strong></td>'), out);
});

test('引用和分隔线', () => {
  const out = ui.renderMarkdown('> 资料原文\n\n---\n结尾');
  assert.ok(out.includes('<blockquote><p>资料原文</p></blockquote>'), out);
  assert.ok(out.includes('<hr>'), out);
});

test('段落内换行变成 <br>', () => {
  assert.equal(ui.renderMarkdown('第一行\n第二行'), '<p>第一行<br>第二行</p>');
});

test('思考段落不显示', () => {
  assert.equal(ui.renderMarkdown('<think>先想想</think>结论是 A'), '<p>结论是 A</p>');
  assert.equal(ui.renderMarkdown('结论<think>还没想完'), '<p>结论</p>');
});

test('空输入', () => {
  assert.equal(ui.renderMarkdown(''), '');
  assert.equal(ui.renderMarkdown(null), '');
});

test('超长、病态输入不卡死', () => {
  const t0 = Date.now();
  ui.renderMarkdown('*'.repeat(20000) + '\n' + '['.repeat(5000) + '\n' + '> '.repeat(2000) + 'x\n' + '- '.repeat(3000));
  ui.renderMarkdown('| a | b |\n|---|---|\n' + '| 1 | 2 |\n'.repeat(3000));
  ui.renderMarkdown('[' + 'a'.repeat(400) + '](' + 'b'.repeat(30000));
  assert.ok(Date.now() - t0 < 3000, '耗时 ' + (Date.now() - t0) + 'ms');
});

console.log('工具动作名');

function pick(o) { return { verb: o.verb, target: o.target }; }

test('常用工具显示成中文动作和对象', () => {
  assert.deepEqual(pick(ui.toolInfo('read_file', { path: '报告/Q3.md' })), { verb: '读取文件', target: '报告/Q3.md' });
  assert.deepEqual(pick(ui.toolInfo('excel_diff', '{"path_a":"销售.xlsx","path_b":"财务.xlsx"}')), { verb: '对账两张表', target: '销售.xlsx 与 财务.xlsx' });
  assert.deepEqual(pick(ui.toolInfo('write_pptx', { path: '季度分析.pptx' })), { verb: '生成 PPT', target: '季度分析.pptx' });
  assert.deepEqual(pick(ui.toolInfo('list_dir', { path: '.' })), { verb: '查看文件夹', target: '整个文件夹' });
  assert.equal(ui.toolInfo('kb_search', { query: '退货政策' }).target, '“退货政策”');
  assert.equal(ui.toolInfo('move_file', { src: 'a.txt', dst: 'b/a.txt' }).target, 'a.txt → b/a.txt');
});

test('按风险换动作名：覆盖、新建', () => {
  assert.equal(ui.toolInfo('write_file', { path: 'a.md' }, 'overwrite').verb, '覆盖文件');
  assert.equal(ui.toolInfo('write_file', { path: 'a.md' }, 'create').verb, '新建文件');
  assert.equal(ui.toolInfo('write_docx', { path: 'a.docx' }, 'overwrite').verb, '覆盖 Word 文档');
  assert.equal(ui.toolInfo('delete_file', { path: 'a.md' }, 'delete').verb, '删除文件');
});

test('所有已知工具都有中文名，不露英文工具名', () => {
  const names = ['list_dir', 'read_file', 'write_file', 'edit_file', 'search_files', 'move_file', 'delete_file',
    'excel_inspect', 'excel_read', 'excel_diff', 'write_xlsx', 'write_docx', 'write_pptx', 'kb_add', 'kb_search',
    'run_python', 'web_fetch', 'web_search', 'todo_write', 'ask_user', 'memory_save', 'schedule_create', 'skill_load', 'spawn_agent'];
  for (const n of names) {
    const info = ui.toolInfo(n, {});
    assert.ok(/[\u4e00-\u9fa5]/.test(info.verb), n + ' → ' + info.verb);
    assert.ok(!info.verb.includes(n) && !info.target.includes(n), n + ' 露出了英文名');
  }
});

test('连接器工具和未知工具有兜底', () => {
  assert.equal(ui.toolInfo('mcp_ticket_create', {}).verb, '调用外部系统');
  assert.equal(ui.toolInfo('mcp_ticket_create', {}).target, 'ticket create');
  assert.equal(ui.toolInfo('whatever', {}).verb, '使用工具');
});

test('参数是坏 JSON、null、数组时不报错', () => {
  assert.equal(ui.toolInfo('read_file', '{坏的').verb, '读取文件');
  assert.equal(ui.toolInfo('read_file', null).target, '');
  assert.equal(ui.toolInfo('excel_read', []).target, '');
});

test('长对象被截短', () => {
  assert.ok(ui.toolInfo('run_python', { code: 'import x\n' + 'y = 1 + '.repeat(100) }).target.length <= 60);
});

test('计划和提问不在时间线里单独显示', () => {
  assert.equal(ui.toolInfo('todo_write', { items: [] }).hidden, true);
  assert.equal(ui.toolInfo('ask_user', { question: '?' }).hidden, true);
  assert.equal(ui.toolInfo('read_file', {}).hidden, false);
});

test('状态文字', () => {
  const want = { waiting_approval: '等你批准', waiting_input: '等你回答', paused: '已暂停', done: '已完成',
    failed: '出错', cancelled: '已取消', running: '运行中', idle: '未开始' };
  for (const k of Object.keys(want)) assert.equal(ui.statusText(k), want[k]);
});

console.log('事件归并');

let seq = 0;
const ev = (type, payload) => ({ seq: ++seq, session_id: 's1', type, payload, ts: 1700000000 + seq });
const delta = (text) => ({ seq: null, session_id: 's1', type: 'assistant_delta', payload: { text } });

test('一轮完整流程：提问 → 流式 → 工具 → 批准 → 结果 → 成品 → 完成', () => {
  seq = 0;
  const v = ui.newView({ id: 's1', status: 'idle' });
  ui.reduceEvent(v, ev('user_message', { text: '把两张表对一下账，差异列出来' }));
  ui.reduceEvent(v, ev('status', { status: 'running' }));
  ui.reduceEvent(v, delta('我先'));
  ui.reduceEvent(v, delta('看看表格。'));
  assert.equal(v.items.length, 2);
  assert.equal(v.items[1].kind, 'assistant');
  assert.equal(v.items[1].streaming, true);
  assert.equal(v.items[1].text, '我先看看表格。');
  assert.equal(v.title, '把两张表对一下账，差异列出来');

  ui.reduceEvent(v, ev('assistant_message', { content: '我先看看表格。', tool_calls: [
    { id: 'c1', name: 'excel_inspect', arguments: { path: '销售.xlsx' } },
    { id: 'c2', name: 'write_file', arguments: '{"path":"差异.md","content":"x"}' }] }));
  assert.equal(v.items.length, 4, '流式气泡被正式消息替换，不重复');
  assert.equal(v.items[1].streaming, false);
  assert.equal(v.items[2].kind, 'tool');
  assert.equal(v.items[2].state, 'planned');
  assert.equal(v.items[3].args.path, '差异.md', '字符串参数被解析');
  assert.equal(v.steps, 1);

  ui.reduceEvent(v, ev('tool_started', { action_id: 'a1', call_id: 'c1', tool: 'excel_inspect', args: { path: '销售.xlsx' } }));
  assert.equal(v.items[2].state, 'running');
  ui.reduceEvent(v, ev('tool_result', { call_id: 'c1', action_id: 'a1', tool: 'excel_inspect', ok: true, content: '{"sheets":[]}' }));
  assert.equal(v.items[2].state, 'done');

  ui.reduceEvent(v, ev('approval_requested', { action_id: 'a2', call_id: 'c2', tool: 'write_file', args: { path: '差异.md', content: 'x' }, risk: 'overwrite' }));
  ui.reduceEvent(v, ev('status', { status: 'waiting_approval' }));
  const att = ui.attention(v);
  assert.equal(att.approvals.length, 1);
  assert.equal(att.approvals[0].actionId, 'a2');
  assert.equal(att.approvals[0].risk, 'overwrite');

  ui.reduceEvent(v, ev('approval_decided', { action_id: 'a2', allow: true, always: false }));
  assert.equal(v.items[3].state, 'approved');
  assert.equal(ui.attention(v).approvals.length, 0);
  ui.reduceEvent(v, ev('status', { status: 'running' }));
  ui.reduceEvent(v, ev('tool_started', { action_id: 'a2', call_id: 'c2', tool: 'write_file', args: {} }));
  ui.reduceEvent(v, ev('artifact', { id: 'f1', path: '差异.md', version: 2, note: '覆盖' }));
  ui.reduceEvent(v, ev('tool_result', { call_id: 'c2', action_id: 'a2', tool: 'write_file', ok: true, content: 'ok', truncated: true }));
  assert.equal(v.items[3].state, 'done');
  assert.equal(v.items[3].truncated, true);
  assert.deepEqual(Object.keys(v.artifacts), ['差异.md']);
  assert.equal(v.artifacts['差异.md'][0].version, 2);

  ui.reduceEvent(v, ev('todo', { items: [{ text: '看表', status: 'done' }, { text: '对账', status: 'doing' }] }));
  assert.equal(v.todo.length, 2);

  ui.reduceEvent(v, ev('assistant_message', { content: '对完了，差异 3 处。', tool_calls: [], endpoint: 'GX10-A' }));
  ui.reduceEvent(v, ev('status', { status: 'done' }));
  assert.equal(v.status, 'done');
  assert.equal(v.endpoint, 'GX10-A');
  assert.equal(v.items[v.items.length - 1].text, '对完了，差异 3 处。');
  const keys = v.items.map(i => i.key);
  assert.equal(new Set(keys).size, keys.length, 'key 不重复');
});

test('每次变化都让条目版本号变大（界面靠它增量更新）', () => {
  seq = 0;
  const v = ui.newView({ id: 's1' });
  ui.reduceEvent(v, ev('assistant_message', { content: '', tool_calls: [{ id: 'c1', name: 'read_file', arguments: {} }] }));
  const t = v.items[0];
  const v0 = t.v;
  ui.reduceEvent(v, ev('tool_started', { action_id: 'a1', call_id: 'c1', tool: 'read_file', args: {} }));
  const v1 = t.v;
  ui.reduceEvent(v, ev('tool_result', { call_id: 'c1', action_id: 'a1', ok: true, content: 'x' }));
  assert.ok(v0 < v1 && v1 < t.v);
});

test('重复的事件（重连补发）被忽略', () => {
  seq = 0;
  const v = ui.newView({ id: 's1' });
  const e1 = ev('user_message', { text: '你好' });
  ui.reduceEvent(v, e1);
  ui.reduceEvent(v, e1);
  ui.reduceEvent(v, Object.assign({}, e1));
  assert.equal(v.items.length, 1);
  assert.equal(v.lastSeq, 1);
});

test('中途改要求：没落账的流式片段被丢弃，旧动作显示"未执行"', () => {
  seq = 0;
  const v = ui.newView({ id: 's1', status: 'running' });
  ui.reduceEvent(v, ev('user_message', { text: '做 A' }));
  ui.reduceEvent(v, ev('assistant_message', { content: '', tool_calls: [{ id: 'c1', name: 'delete_file', arguments: { path: 'a.txt' } }] }));
  ui.reduceEvent(v, ev('approval_requested', { action_id: 'a1', call_id: 'c1', tool: 'delete_file', args: { path: 'a.txt' }, risk: 'delete' }));
  ui.reduceEvent(v, delta('正在想'));
  ui.reduceEvent(v, ev('user_message', { text: '算了，改成做 B' }));
  assert.ok(!v.items.some(i => i.streaming), '流式气泡已丢弃');
  ui.reduceEvent(v, ev('tool_result', { call_id: 'c1', action_id: 'a1', tool: 'delete_file', ok: false, content: '未执行：用户更改了要求' }));
  const t = v.items.find(i => i.kind === 'tool');
  assert.equal(t.state, 'voided');
  assert.equal(ui.attention(v).approvals.length, 0);
});

test('拒绝、结果未知、普通失败分别标出', () => {
  seq = 0;
  const v = ui.newView({ id: 's1' });
  ui.reduceEvent(v, ev('assistant_message', { content: '', tool_calls: [
    { id: 'c1', name: 'write_file', arguments: {} }, { id: 'c2', name: 'run_python', arguments: {} }, { id: 'c3', name: 'read_file', arguments: {} }] }));
  ui.reduceEvent(v, ev('tool_result', { call_id: 'c1', ok: false, content: '用户拒绝了这个操作。不要换一种方式重试' }));
  ui.reduceEvent(v, ev('tool_result', { call_id: 'c2', ok: false, content: '此动作在执行中途程序中断，结果未知。' }));
  ui.reduceEvent(v, ev('tool_result', { call_id: 'c3', ok: false, content: '找不到文件：x' }));
  assert.deepEqual(v.items.map(i => i.state), ['denied', 'unknown', 'failed']);
});

test('提问与回答', () => {
  seq = 0;
  const v = ui.newView({ id: 's1' });
  ui.reduceEvent(v, ev('assistant_message', { content: '', tool_calls: [{ id: 'q1', name: 'ask_user', arguments: { question: '用哪张表？' } }] }));
  ui.reduceEvent(v, ev('question', { call_id: 'q1', question: '用哪张表？', options: ['销售', '财务'] }));
  ui.reduceEvent(v, ev('status', { status: 'waiting_input' }));
  let att = ui.attention(v);
  assert.ok(att.question);
  assert.deepEqual(att.question.options, ['销售', '财务']);
  ui.reduceEvent(v, ev('answer', { call_id: 'q1', text: '销售' }));
  ui.reduceEvent(v, ev('status', { status: 'running' }));
  att = ui.attention(v);
  assert.equal(att.question, null);
  const q = v.items.find(i => i.kind === 'question');
  assert.equal(q.answered, true);
  assert.equal(q.answer, '销售');
});

test('暂停原因和程序重启', () => {
  seq = 0;
  const v = ui.newView({ id: 's1', status: 'running' });
  ui.reduceEvent(v, ev('status', { status: 'paused', reason: '程序重启' }));
  assert.equal(v.status, 'paused');
  assert.equal(v.reason, '程序重启');
  assert.equal(ui.attention(v).paused, true);
  ui.reduceEvent(v, ev('status', { status: 'running' }));
  assert.equal(v.reason, '');
});

test('出错时丢弃流式片段并显示错误', () => {
  seq = 0;
  const v = ui.newView({ id: 's1', status: 'running' });
  ui.reduceEvent(v, delta('半句'));
  ui.reduceEvent(v, ev('error', { text: '模型不可用：连接超时' }));
  ui.reduceEvent(v, ev('status', { status: 'paused', reason: '模型不可用：连接超时' }));
  assert.equal(v.items.length, 1);
  assert.equal(v.items[0].tone, 'error');
});

test('从接口数据重建视图：pending 补出待批准卡片，成品按版本排', () => {
  seq = 0;
  const events = [
    ev('user_message', { text: '删掉旧文件' }),
    ev('assistant_message', { content: '', tool_calls: [{ id: 'c9', name: 'delete_file', arguments: { path: 'old.txt' } }] }),
  ];
  const v = ui.buildView({ id: 's1', title: '删旧文件', status: 'paused', meta: { reason: '程序重启', todo: [{ text: 'x', status: 'pending' }] } },
    events, [{ id: 'a9', call_id: 'c9', tool: 'delete_file', args: { path: 'old.txt' }, risk: 'delete', state: 'awaiting' }],
    [{ id: 'f2', path: 'r.docx', version: 2, created: 2 }, { id: 'f1', path: 'r.docx', version: 1, created: 1 }]);
  assert.equal(v.title, '删旧文件');
  assert.equal(v.status, 'paused');
  assert.equal(v.reason, '程序重启');
  assert.equal(v.todo.length, 1);
  const t = v.items.find(i => i.kind === 'tool');
  assert.equal(t.state, 'awaiting');
  assert.equal(t.actionId, 'a9');
  assert.equal(t.risk, 'delete');
  assert.deepEqual(v.artifacts['r.docx'].map(a => a.version), [1, 2]);
});

test('乱七八糟的事件不让归并崩溃', () => {
  const v = ui.newView({ id: 's1' });
  for (const bad of [null, undefined, 1, 'x', {}, { type: 'assistant_message' }, { type: 'assistant_message', payload: { tool_calls: [null, 1, {}] } },
    { type: 'tool_result', payload: null }, { type: 'artifact', payload: {} }, { type: 'todo', payload: { items: 'x' } },
    { type: 'question', payload: { options: 'x' } }, { type: 'unknown_type', payload: {} }]) {
    ui.reduceEvent(v, bad);
  }
  assert.ok(Array.isArray(v.items));
  assert.deepEqual(v.todo, []);
});

console.log('任务列表');

test('状态事件更新列表，并按更新时间重排', () => {
  const list = [{ id: 'a', status: 'done', updated: 10 }, { id: 'b', status: 'running', updated: 5 }];
  const r = ui.applyListEvent(list, { seq: 3, session_id: 'b', type: 'status', payload: { status: 'waiting_approval' }, ts: 20 });
  assert.equal(r.changed, true);
  assert.equal(list[0].id, 'b');
  assert.equal(list[0].status, 'waiting_approval');
});

test('未知任务的事件要求刷新列表；流式片段不动列表', () => {
  const list = [{ id: 'a', status: 'done', updated: 10 }];
  assert.equal(ui.applyListEvent(list, { session_id: 'zz', type: 'user_message', payload: {}, ts: 1 }).unknown, true);
  assert.equal(ui.applyListEvent(list, { session_id: 'a', type: 'assistant_delta', payload: { text: 'x' }, ts: 99 }).changed, false);
  assert.equal(list[0].updated, 10);
});

console.log('小工具');

test('岗位分组保持原顺序', () => {
  const g = ui.groupTemplates([{ role: '财务', title: 'a', prompt: '1' }, { role: '销售', title: 'b', prompt: '2' }, { role: '财务', title: 'c', prompt: '3' }, { role: 'x' }]);
  assert.deepEqual(g.map(x => x.role), ['财务', '销售']);
  assert.equal(g[0].items.length, 2);
});

test('命令面板匹配', () => {
  const items = [{ label: '打开设置' }, { label: '新建任务' }, { label: '季度对账' }];
  assert.deepEqual(ui.filterCommands(items, '对账').map(x => x.label), ['季度对账']);
  assert.equal(ui.filterCommands(items, '').length, 3);
  assert.deepEqual(ui.filterCommands(items, '设').map(x => x.label), ['打开设置']);
});

test('从地址取令牌、拼接口地址', () => {
  assert.equal(ui.tokenFrom('?t=abc%2B1&x=2'), 'abc+1');
  assert.equal(ui.tokenFrom('?x=1'), '');
  assert.equal(ui.apiUrl('/api/file', { workspace: 'w1', path: '报告/a b.docx', download: '' }), '/api/file?workspace=w1&path=%E6%8A%A5%E5%91%8A%2Fa%20b.docx');
  assert.equal(ui.apiUrl('/api/events', { since: '', t: 'k' }), '/api/events?t=k');
});

test('时间和大小', () => {
  const now = new Date(2026, 10, 20, 15, 0).getTime();
  assert.equal(ui.formatTime(new Date(2026, 10, 20, 9, 5).getTime() / 1000, now), '今天 09:05');
  assert.equal(ui.formatTime(new Date(2026, 10, 19, 23, 0).getTime() / 1000, now), '昨天 23:00');
  assert.equal(ui.formatTime(new Date(2026, 1, 3, 8, 0).getTime() / 1000, now), '2月3日 08:00');
  assert.equal(ui.relativeTime(now / 1000 - 30, now), '刚刚');
  assert.equal(ui.relativeTime(now / 1000 - 600, now), '10 分钟前');
  assert.equal(ui.formatSize(2048), '2.0 KB');
  assert.equal(ui.fileName('a/b/季度分析.pptx'), '季度分析.pptx');
  assert.equal(ui.prettyResult('{"a":1}'), '{\n  "a": 1\n}');
  assert.equal(ui.prettyResult('不是 JSON'), '不是 JSON');
});

console.log('记忆、搜索、技能');

test('新工具的中文名和对象；记忆旧工具名保留', () => {
  assert.deepEqual(pick(ui.toolInfo('memory', { action: 'add', target: 'user', content: '报告先给结论' })), { verb: '更新记忆', target: '记下 “报告先给结论”' });
  assert.equal(ui.toolInfo('memory', { action: 'replace', content: '新的', old_text: '旧的' }).target, '改成 “新的”');
  assert.equal(ui.toolInfo('memory', { action: 'remove', old_text: '过时的' }).target, '删掉 “过时的”');
  assert.equal(ui.toolInfo('memory', {}).target, '');
  assert.deepEqual(pick(ui.toolInfo('session_search', { query: '对账' })), { verb: '查找以前的对话', target: '“对账”' });
  assert.equal(ui.toolInfo('session_search', { session_id: 's_1' }).target, '对话 s_1');
  assert.deepEqual(pick(ui.toolInfo('skill_save', { name: 'monthly-reconcile' })), { verb: '保存技能', target: 'monthly-reconcile' });
  assert.equal(ui.toolInfo('memory_save', { text: 'x' }).verb, '记住一件事');
  assert.equal(ui.riskLabel('skill'), '技能');
  assert.equal(ui.riskLabel('memory'), '记忆');
});

test('记忆提示：按 op 和 origin 出不同文字', () => {
  assert.equal(ui.memoryNoteText({ op: 'add', text: '财务总监' }), '记住了：财务总监');
  assert.equal(ui.memoryNoteText({ op: 'replace', text: '新口径' }), '更新了记忆：新口径');
  assert.equal(ui.memoryNoteText({ op: 'remove', text: '旧口径' }), '删掉了一条记忆：旧口径');
  assert.equal(ui.memoryNoteText({ op: 'add', origin: 'review', text: 'a' }), '自动整理 · 记住了：a');
  assert.equal(ui.memoryNoteText({ text: '旧事件' }), '记住了：旧事件');
  assert.equal(ui.memoryNoteText(null), '记住了：');
});

test('memory 事件归并：新格式带撤销编号，旧格式按"记住了"', () => {
  seq = 100;
  const v = ui.newView({ id: 's1' });
  ui.reduceEvent(v, ev('memory', { op: 'replace', id: 'm1', target: 'user', text: '新的', before: '旧的', log_id: 7, origin: 'chat' }));
  ui.reduceEvent(v, ev('memory', { op: 'remove', text: '没用了', log_id: 8, origin: 'review' }));
  ui.reduceEvent(v, ev('memory', { text: '老格式' }));
  ui.reduceEvent(v, ev('memory', { op: '奇怪', text: 'x', log_id: '' }));
  const ms = v.items.filter(i => i.kind === 'memory');
  assert.equal(ms.length, 4);
  assert.equal(ms[0].op, 'replace');
  assert.equal(ms[0].before, '旧的');
  assert.equal(ms[0].logId, 7, '撤销编号保持原来的类型');
  assert.equal(ui.memoryNoteText(ms[0]), '更新了记忆：新的');
  assert.equal(ui.memoryNoteText(ms[1]), '自动整理 · 删掉了一条记忆：没用了');
  assert.equal(ms[2].logId, null, '旧事件没有撤销');
  assert.equal(ui.memoryNoteText(ms[2]), '记住了：老格式');
  assert.equal(ms[3].op, 'add');
  assert.equal(ms[3].logId, null);
  assert.ok(ms.every(m => typeof m.key === 'string' && m.v === 0));
});

test('memory_undo 事件：对应的记忆提示标成已撤销，本身不显示', () => {
  seq = 400;
  const v = ui.newView({ id: 's1' });
  ui.reduceEvent(v, ev('memory', { op: 'add', id: 'm1', target: 'user', text: 'A', before: null, log_id: 21, origin: 'chat' }));
  ui.reduceEvent(v, ev('memory', { op: 'add', id: 'm2', target: 'user', text: 'B', before: null, log_id: 22, origin: 'chat' }));
  const n0 = v.items.length;
  const m1 = v.items.find(i => i.logId === 21), m2 = v.items.find(i => i.logId === 22);
  assert.equal(m1.undone, false);
  const v0 = m1.v;
  ui.reduceEvent(v, ev('memory_undo', { log_id: 21, memory_id: 'm1' }));
  assert.equal(v.items.length, n0, '本身不显示');
  assert.equal(m1.undone, true);
  assert.ok(m1.v > v0, '版本号变大，界面会重画');
  assert.equal(m2.undone, false, '别的不受影响');
  // 重连补发同一条（seq 不变）不出错、不重复处理
  const again = { seq: seq, session_id: 's1', type: 'memory_undo', payload: { log_id: 21 } };
  const v1 = m1.v;
  ui.reduceEvent(v, again);
  assert.equal(m1.v, v1);
  // 找不到对应提示、缺 log_id、payload 乱七八糟：忽略
  for (const p of [{ log_id: 999 }, {}, { log_id: null }, { log_id: '' }, null]) ui.reduceEvent(v, ev('memory_undo', p));
  assert.equal(v.items.length, n0);
  assert.equal(m2.undone, false);
  // 编号类型不同（字符串和数字）也能对上
  ui.reduceEvent(v, ev('memory_undo', { log_id: '22' }));
  assert.equal(m2.undone, true);
});

test('memory_undo 先到、对应的 memory 事件后到：仍然显示已撤销', () => {
  // 实时推送可能先于补发的旧事件到达，所以这里不带 seq，模拟乱序
  const v = ui.newView({ id: 's1' });
  ui.reduceEvent(v, { session_id: 's1', type: 'memory_undo', payload: { log_id: 31, memory_id: 'm3' } });
  assert.equal(v.items.length, 0);
  ui.reduceEvent(v, { session_id: 's1', type: 'memory', payload: { op: 'add', text: 'C', log_id: 31, origin: 'chat' } });
  ui.reduceEvent(v, { session_id: 's1', type: 'memory', payload: { op: 'add', text: 'D', log_id: 32, origin: 'chat' } });
  const ms = v.items.filter(i => i.kind === 'memory');
  assert.deepEqual(ms.map(m => m.undone), [true, false]);
  // 用整串事件重建视图（刷新页面）时也一样
  const b = ui.buildView({ id: 's1', status: 'done' }, [
    { seq: 1, session_id: 's1', type: 'memory', payload: { op: 'replace', text: 'E', before: 'e', log_id: 41 } },
    { seq: 2, session_id: 's1', type: 'memory_undo', payload: { log_id: 41 } }], [], []);
  assert.deepEqual(b.items.map(i => [i.kind, i.undone]), [['memory', true]]);
  // 不让任务跳到列表顶上
  const list = [{ id: 'a', updated: 10 }, { id: 's1', updated: 5 }];
  assert.equal(ui.applyListEvent(list, { seq: 9, session_id: 's1', type: 'memory_undo', payload: { log_id: 41 }, ts: 99 }).changed, false);
});

test('skill 和 suggestion 事件；存成技能后提示变成已完成', () => {
  seq = 200;
  const v = ui.newView({ id: 's1' });
  ui.reduceEvent(v, ev('suggestion', { kind: 'save_skill', text: '这次的做法可以存成技能，下次同类的事直接照做。' }));
  ui.reduceEvent(v, ev('suggestion', { kind: 'save_skill' }));
  ui.reduceEvent(v, ev('suggestion', { kind: 'unknown' }));
  ui.reduceEvent(v, ev('suggestion', { kind: 'other', text: '别的提示' }));
  let sg = v.items.filter(i => i.kind === 'suggest');
  assert.equal(sg.length, 3, '既没有 text 又不认识的 kind 不显示');
  assert.equal(sg[0].what, 'save_skill');
  assert.equal(sg[1].text, '这次的做法可以存成技能，下次同类的事直接照做。', '没有 text 时用默认文字');
  assert.equal(sg[0].done, false);
  const v0 = sg[0].v;
  ui.reduceEvent(v, ev('skill', { name: 'monthly-reconcile', description: '月度对账', updated: true }));
  const sk = v.items.find(i => i.kind === 'skill');
  assert.equal(sk.name, 'monthly-reconcile');
  assert.equal(sk.updated, true);
  sg = v.items.filter(i => i.kind === 'suggest');
  assert.equal(sg[0].done, true);
  assert.ok(sg[0].v > v0, '版本号变大，界面会重画');
  assert.equal(sg[2].done, false, '别的提示不受影响');
});

test('后台回顾记下的东西不让任务跳到列表顶上', () => {
  const list = [{ id: 'a', status: 'done', updated: 10 }, { id: 'b', status: 'done', updated: 5 }];
  let r = ui.applyListEvent(list, { seq: 1, session_id: 'b', type: 'memory', payload: { origin: 'review', text: 'x' }, ts: 99 });
  assert.equal(r.changed, false);
  assert.equal(list[0].id, 'a');
  r = ui.applyListEvent(list, { seq: 2, session_id: 'b', type: 'suggestion', payload: { kind: 'save_skill' }, ts: 99 });
  assert.equal(r.changed, false);
  r = ui.applyListEvent(list, { seq: 3, session_id: 'b', type: 'memory', payload: { origin: 'chat', text: 'x' }, ts: 99 });
  assert.equal(r.changed, true);
  assert.equal(list[0].id, 'b');
});

test('用量文字：没有就不显示，满了说已满', () => {
  assert.equal(ui.usageText({ used: 205, budget: 500, pct: 41 }), '已用 41%');
  assert.equal(ui.usageText({ used: 250, budget: 500 }), '已用 50%', '没有 pct 时自己算');
  assert.equal(ui.usageText({ pct: 100 }), '已满');
  assert.equal(ui.usageText({ pct: 120 }), '已满');
  assert.equal(ui.usageText({ pct: 0 }), '已用 0%');
  for (const x of [null, undefined, {}, 'x', { pct: 'abc' }, { budget: 0, used: 3 }]) assert.equal(ui.usageText(x), '', JSON.stringify(x));
  assert.equal(ui.usagePct({ pct: 41 }), 41);
  assert.equal(ui.usagePct({ pct: 130 }), 100);
  assert.equal(ui.usagePct({ used: 1, budget: 4 }), 25);
  assert.equal(ui.usagePct(null), 0);
});

test('来源和本子的显示名', () => {
  assert.equal(ui.originLabel('user'), '你添加的');
  assert.equal(ui.originLabel('chat'), '对话中记下');
  assert.equal(ui.originLabel('review'), '自动整理');
  assert.equal(ui.originLabel(undefined), '你添加的');
  assert.equal(ui.targetLabel('user'), '关于我');
  assert.equal(ui.targetLabel('workspace'), '这个文件夹');
});

test('日期：秒数和 YYYY-MM-DD 都认', () => {
  const now = new Date(2026, 10, 20, 15, 0).getTime();
  assert.equal(ui.shortDate(new Date(2026, 10, 20, 9, 0).getTime() / 1000, now), '今天');
  assert.equal(ui.shortDate(new Date(2026, 10, 19, 9, 0).getTime() / 1000, now), '昨天');
  assert.equal(ui.shortDate(new Date(2026, 1, 3).getTime() / 1000, now), '2月3日');
  assert.equal(ui.shortDate('2026-11-18', now), '11月18日');
  assert.equal(ui.shortDate('2025-11-18', now), '2025年11月18日');
  assert.equal(ui.shortDate('上周', now), '上周', '认不出的字符串原样显示');
  assert.equal(ui.shortDate(null, now), '');
  assert.equal(ui.shortDate(0, now), '');
});

test('记忆列表：新格式分两栏、带用量和开关', () => {
  const d = {
    memories: [
      { id: 'm2', target: 'user', text: '报告先给结论', origin: 'review', session_id: 's9', created: 20, updated: 30 },
      { id: 'm1', target: 'user', text: '财务总监', origin: 'user', created: 10, updated: null },
      { id: 'm3', target: 'workspace', workspace_id: 'w1', text: '对账表在对账/', origin: 'chat', session_id: 's8', created: 5 }],
    usage: { user: { used: 100, budget: 500, pct: 20 }, workspace: { used: 0, budget: 800, pct: 0 } },
    settings: { enabled: true, auto: false }
  };
  const r = ui.normalizeMemories(d, 'w1');
  assert.equal(r.legacy, false);
  assert.deepEqual(r.user.map(m => m.id), ['m1', 'm2'], '按 created 升序');
  assert.equal(r.user[0].updated, 10, '没有 updated 时用 created');
  assert.equal(r.user[1].origin, 'review');
  assert.equal(r.user[1].session_id, 's9');
  assert.equal(r.workspace.length, 1);
  assert.equal(r.workspace[0].target, 'workspace');
  assert.equal(r.usage.user.pct, 20);
  assert.equal(r.usage.workspace.pct, 0);
  assert.deepEqual(r.settings, { enabled: true, auto: false });
});

test('记忆列表：缺用量、缺开关时降级', () => {
  const r = ui.normalizeMemories({ memories: [{ id: 'a', target: 'user', text: 'x', created: 1 }] }, 'w1');
  assert.equal(r.legacy, false);
  assert.equal(r.usage.user, null);
  assert.equal(r.usage.workspace, null);
  assert.equal(r.settings, null);
  assert.equal(ui.usageText(r.usage.user), '');
  const r2 = ui.normalizeMemories({ memories: [], usage: { user: { pct: 5 } } }, 'w1');
  assert.equal(r2.usage.user.pct, 5);
  assert.equal(r2.usage.workspace, null);
});

test('记忆列表：旧格式（直接是列表，按 workspace_id 分，按 source 推来源）', () => {
  const old = [
    { id: 'm9', workspace_id: 'w1', text: '本文件夹的', source: 'user', created: 50 },
    { id: 'm8', workspace_id: null, text: '通用的', source: 's_abc', created: 40 },
    { id: 'm7', workspace_id: 'w2', text: '别的文件夹', source: 'user', created: 30 }];
  const r = ui.normalizeMemories(old, 'w1');
  assert.equal(r.legacy, true);
  assert.deepEqual(r.user.map(m => m.text), ['通用的']);
  assert.equal(r.user[0].origin, 'chat');
  assert.equal(r.user[0].session_id, 's_abc');
  assert.deepEqual(r.workspace.map(m => m.text), ['本文件夹的'], '别的文件夹的不显示');
  assert.equal(r.workspace[0].origin, 'user');
  assert.equal(r.usage.user, null);
  assert.equal(r.settings, null);
});

test('记忆列表：乱七八糟的返回不崩', () => {
  for (const d of [null, undefined, 'x', 42, {}, { memories: 'x' }, [null, 1, 'a', { text: '没有 id' }], { memories: [{ id: 1, text: null }] }]) {
    const r = ui.normalizeMemories(d, 'w1');
    assert.ok(Array.isArray(r.user) && Array.isArray(r.workspace), JSON.stringify(d));
  }
  assert.equal(ui.normalizeMemories({ memories: [{ id: 1, text: null }] }, 'w1').user[0].text, '');
});

test('最近的改动：每种 op 的文字', () => {
  const rows = ui.normalizeLog({ log: [
    { id: 3, ts: 30, op: 'replace', target: 'user', before: '旧', after: '新', origin: 'chat', session_id: 's1', undone: 0 },
    { id: 2, ts: 20, op: 'remove', target: 'workspace', before: '删掉的', after: null, origin: 'user', undone: 1 },
    { id: 1, ts: 10, op: 'add', target: 'user', before: null, after: '加的', origin: 'review' },
    null, { op: 'add' }] });
  assert.equal(rows.length, 3, '没有 id 的行丢掉');
  assert.deepEqual([rows[0].opLabel, rows[0].text, rows[0].before, rows[0].target, rows[0].origin, rows[0].undone],
    ['修改', '新', '旧', '关于我', '对话中记下', false]);
  assert.deepEqual([rows[1].opLabel, rows[1].text, rows[1].before, rows[1].target, rows[1].undone], ['删除', '删掉的', '', '这个文件夹', true]);
  assert.deepEqual([rows[2].opLabel, rows[2].text, rows[2].origin], ['添加', '加的', '自动整理']);
  assert.deepEqual(ui.normalizeLog([{ id: 1, op: 'add', after: 'x' }]).map(r => r.text), ['x'], '直接是列表也认');
  assert.deepEqual(ui.normalizeLog(null), []);
});

test('技能列表：标出来源，没有 source 的当内置，我的排前面', () => {
  const r = ui.normalizeSkills([{ name: 'excel-recon', description: '对账' }, { name: 'my-weekly', description: '周报', source: 'user' },
    { name: 'deck', source: 'builtin' }, { description: '没名字' }, null]);
  assert.deepEqual(r.map(s => s.name + ':' + s.source), ['my-weekly:user', 'excel-recon:builtin', 'deck:builtin']);
  assert.equal(r[2].description, '');
  assert.deepEqual(ui.normalizeSkills({ skills: [{ name: 'a', source: 'user' }] }).map(s => s.source), ['user']);
  assert.deepEqual(ui.normalizeSkills('x'), []);
});

test('搜索：查询词整理、结果整理', () => {
  assert.equal(ui.searchQuery('  月度   对账 \n'), '月度 对账');
  assert.equal(ui.searchQuery(null), '');
  assert.equal(ui.searchQuery('x'.repeat(500)).length, 200);
  const r = ui.normalizeHits({ hits: [
    { session_id: 's1', title: '十月对账', date: '2026-11-18', matches: [
      { seq: 3, role: '用户', snippet: '把十月的\n销售和   财务对账' }, { seq: 4, role: 'assistant', snippet: '差异 3 笔' }, { seq: 5, role: '用户' }] },
    { session_id: 's1', title: '重复的' },
    { title: '没有任务号' },
    { session_id: 's2', matches: 'x' }] });
  assert.equal(r.hits.length, 2);
  assert.equal(r.hits[0].matches.length, 2, '没有片段的命中丢掉');
  assert.equal(r.hits[0].matches[0].role, '你');
  assert.equal(r.hits[0].matches[0].snippet, '把十月的 销售和 财务对账');
  assert.equal(r.hits[0].matches[1].role, 'Agent');
  assert.equal(r.hits[1].title, '未命名对话');
  assert.deepEqual(r.hits[1].matches, []);
  assert.equal(r.note, '');
  const empty = ui.normalizeHits({ hits: [], note: '没有找到。可以换个说法再查一次，或者直接问用户。' });
  assert.equal(empty.hits.length, 0);
  assert.ok(empty.note.length > 0);
  assert.deepEqual(ui.normalizeHits(null).hits, []);
  assert.deepEqual(ui.normalizeHits([{ session_id: 'a' }]).hits.map(h => h.session_id), ['a']);
});

test('搜索：命中词标出来，不分大小写，重叠合并', () => {
  const join = parts => parts.map(p => p.hit ? '[' + p.t + ']' : p.t).join('');
  assert.equal(join(ui.highlightParts('销售和财务对账', '对账 财务')), '销售和[财务对账]');
  assert.equal(join(ui.highlightParts('Q3 report and q3 plan', 'q3')), '[Q3] report and [q3] plan');
  assert.equal(join(ui.highlightParts('abcabc', 'bca abc')), '[abcabc]');
  assert.equal(join(ui.highlightParts('没有命中', '对账')), '没有命中');
  assert.deepEqual(ui.highlightParts('', '对账'), []);
  assert.equal(join(ui.highlightParts('<b>x</b>', 'x')), '<b>[x]</b>', '只切文字，不产生 HTML');
  const t0 = Date.now();
  ui.highlightParts('a'.repeat(100000), 'a');
  assert.ok(Date.now() - t0 < 1000);
});

test('搜索结果上方的那句话', () => {
  assert.equal(ui.searchStatusText({ q: '对账', loading: true, hits: null }), '正在查找…');
  assert.equal(ui.searchStatusText({ q: '对账', loading: true, hits: [{}, {}] }), '找到 2 个对话，正在更新…', '旧结果先留着');
  assert.equal(ui.searchStatusText({ q: '对账', loading: false, hits: [], error: ui.MISSING_API }), ui.MISSING_API);
  assert.equal(ui.searchStatusText({ q: '对账', loading: false, hits: [] }), '没有找到包含“对账”的对话。换个说法试试。');
  assert.equal(ui.searchStatusText({ q: '对账', hits: [{}] }), '找到 1 个对话');
  assert.ok(ui.searchStatusText({ q: 'x'.repeat(100), hits: [] }).length < 60, '长查询词截短');
  assert.equal(ui.searchStatusText(null), '没有找到包含“”的对话。换个说法试试。');
});

test('memory 事件：按后端 memory.apply 实际发出的字段归并', () => {
  seq = 300;
  const v = ui.newView({ id: 's1' });
  // agent/memory.py apply()：{op, id, target, text, before, log_id, origin}；remove 时 before 和 text 都是被删的文字
  ui.reduceEvent(v, ev('memory', { op: 'add', id: 'm1', target: 'user', text: '财务总监', before: null, log_id: 11, origin: 'chat' }));
  ui.reduceEvent(v, ev('memory', { op: 'remove', id: 'm1', target: 'workspace', text: '旧口径', before: '旧口径', log_id: 12, origin: 'review' }));
  const ms = v.items.filter(i => i.kind === 'memory');
  assert.deepEqual(ms.map(m => [m.op, m.origin, m.target, m.logId, m.before]),
    [['add', 'chat', 'user', 11, ''], ['remove', 'review', 'workspace', 12, '旧口径']]);
  assert.equal(ui.memoryNoteText(ms[0]), '记住了：财务总监');
  assert.equal(ui.memoryNoteText(ms[1]), '自动整理 · 删掉了一条记忆：旧口径');
  // 重连补发同一条事件不重复显示
  ui.reduceEvent(v, { seq: 301, session_id: 's1', type: 'memory', payload: { op: 'add', text: '财务总监', log_id: 11 } });
  assert.equal(v.items.filter(i => i.kind === 'memory').length, 2);
});

test('最近的改动：后端 memory.log 的行（带 memory_id、undone 是布尔）', () => {
  const rows = ui.normalizeLog({ log: [
    { id: 5, ts: 1700000000, memory_id: 'm1', op: 'add', target: 'workspace', before: null, after: '对账表在对账/', origin: 'chat', session_id: 's_1', undone: true },
    { id: 4, ts: 1699999999, memory_id: 'm2', op: 'replace', target: 'user', before: 'A', after: 'B', origin: 'user', session_id: null, undone: false }] });
  assert.deepEqual(rows.map(r => [r.id, r.opLabel, r.target, r.text, r.undone, r.session_id]),
    [[5, '添加', '这个文件夹', '对账表在对账/', true, 's_1'], [4, '修改', '关于我', 'B', false, null]]);
  assert.equal(ui.describeLog({ id: 1, op: 'add', after: 'x', workspace_id: 'w1' }).target, '这个文件夹', '没有 target 时看 workspace_id');
});

test('搜索：后端 /api/search 实际返回的形状', () => {
  // agent/tools/history.py search()：role 是"用户"/"助手"，date 是 YYYY-MM-DD
  const r = ui.normalizeHits({ hits: [{ session_id: 's_9', title: '（无标题）', date: '2026-11-18',
    matches: [{ seq: 2, role: '助手', snippet: '…对账差异 3 笔…' }] }] });
  assert.equal(r.hits[0].title, '（无标题）');
  assert.equal(r.hits[0].matches[0].role, 'Agent');
  assert.equal(ui.shortDate(r.hits[0].date, new Date(2026, 10, 20).getTime()), '11月18日');
});

test('接口出错的提示：旧版程序没有新接口时给明白话', () => {
  assert.equal(ui.apiErrorMessage(400, { error: '已经记过了', code: 'duplicate' }), '已经记过了');
  assert.equal(ui.apiErrorMessage(409, { error: '这条记忆后来又改过，不能撤销。' }), '这条记忆后来又改过，不能撤销。');
  assert.equal(ui.apiErrorMessage(404, { error: '没有这个接口' }), ui.MISSING_API);
  assert.equal(ui.apiErrorMessage(405, { error: '不支持这个请求方法' }), ui.MISSING_API);
  assert.equal(ui.apiErrorMessage(404, { error: '没有这条记忆' }), '没有这条记忆');
  assert.equal(ui.apiErrorMessage(404, null), '找不到要的东西，可能已经被删除。');
  assert.equal(ui.apiErrorMessage(403, null), '没有权限。请用启动时打印的地址重新打开。');
  assert.equal(ui.apiErrorMessage(500, 'x'), '操作没有成功（500）。');
});

test('导出：地址带令牌，文件名从响应头里取', () => {
  assert.equal(ui.exportUrl('s_1', 'k+1'), '/api/sessions/s_1/export?t=k%2B1');
  assert.equal(ui.dispositionName("attachment; filename*=UTF-8''%E5%8D%81%E6%9C%88%E5%AF%B9%E8%B4%A6.md", 'x.md'), '十月对账.md');
  assert.equal(ui.dispositionName('attachment; filename="report.md"', 'x.md'), 'report.md');
  assert.equal(ui.dispositionName('attachment; filename=plain.md', 'x.md'), 'plain.md');
  assert.equal(ui.dispositionName("attachment; filename*=UTF-8''..%2F..%2Fetc%2Fpasswd", 'x.md'), '.._.._etc_passwd', '路径符号换掉');
  assert.equal(ui.dispositionName("attachment; filename*=UTF-8''%E0%A4%A", '任务.md'), '任务.md', '坏编码用备用名');
  assert.equal(ui.dispositionName(null, '任务.md'), '任务.md');
  assert.equal(ui.dispositionName(null, ''), '对话记录.md');
});

test('界面只在一个地方写 innerHTML', () => {
  const fs = require('fs');
  const js = fs.readFileSync(path.join(here, '..', 'ui', 'app.js'), 'utf8');
  assert.equal((js.match(/\.innerHTML\s*=/g) || []).length, 1);
  assert.ok(!/insertAdjacentHTML|outerHTML\s*=|document\.write/.test(js));
});

console.log('语音输入');

function sine(rate, seconds, freq, amp) {
  const n = Math.round(rate * seconds), out = new Float32Array(n);
  for (let i = 0; i < n; i++) out[i] = amp * Math.sin(2 * Math.PI * freq * i / rate);
  return out;
}
function peak(a, from = 0, to = a.length) { let p = 0; for (let i = from; i < to; i++) p = Math.max(p, Math.abs(a[i])); return p; }

/** 按 WAV 规范重新解析一遍（独立实现，不用被测代码）。 */
function parseWav(buf) {
  const dv = new DataView(buf);
  const tag = (o) => String.fromCharCode(dv.getUint8(o), dv.getUint8(o + 1), dv.getUint8(o + 2), dv.getUint8(o + 3));
  assert.equal(tag(0), 'RIFF');
  assert.equal(tag(8), 'WAVE');
  let off = 12, fmt = null, data = null;
  while (off + 8 <= buf.byteLength) {
    const id = tag(off), size = dv.getUint32(off + 4, true);
    if (id === 'fmt ') fmt = { format: dv.getUint16(off + 8, true), channels: dv.getUint16(off + 10, true), rate: dv.getUint32(off + 12, true),
      byteRate: dv.getUint32(off + 16, true), align: dv.getUint16(off + 20, true), bits: dv.getUint16(off + 22, true) };
    if (id === 'data') { data = []; for (let i = 0; i < size / 2; i++) data.push(dv.getInt16(off + 8 + i * 2, true)); }
    off += 8 + size + (size % 2);
  }
  return { riffSize: dv.getUint32(4, true), fmt, data };
}

test('重采样 48000 → 16000：长度、幅值、频率都合理', () => {
  const x = sine(48000, 1, 440, 0.5);
  const y = ui.resample(x, 48000, 16000);
  assert.equal(y.length, 16000);
  const p = peak(y, 100, y.length - 100);
  assert.ok(p > 0.45 && p <= 0.5001, '峰值 ' + p);
  // 和直接在 16 kHz 生成的正弦比，误差小
  const ref = sine(16000, 1, 440, 0.5);
  let err = 0; for (let i = 100; i < 15900; i++) err = Math.max(err, Math.abs(y[i] - ref[i]));
  assert.ok(err < 0.06, '最大误差 ' + err);
});

test('重采样 44100 → 16000：长度、幅值合理', () => {
  const x = sine(44100, 2, 300, 0.8);
  const y = ui.resample(x, 44100, 16000);
  assert.ok(Math.abs(y.length - 32000) <= 1, '长度 ' + y.length);
  const p = peak(y, 100, y.length - 100);
  assert.ok(p > 0.75 && p <= 0.8001, '峰值 ' + p);
  // 常数信号保持不变
  const c = ui.resample(new Float32Array(4410).fill(0.25), 44100, 16000);
  assert.equal(c.length, 1600);
  assert.ok(c.every(v => Math.abs(v - 0.25) < 1e-6));
});

test('重采样：降采样会压低高于新奈奎斯特频率的成分', () => {
  const y = ui.resample(sine(48000, 0.5, 15000, 0.5), 48000, 16000);
  assert.ok(peak(y, 50, y.length - 50) < 0.3, '峰值 ' + peak(y, 50, y.length - 50));
});

test('重采样：同采样率、升采样、空输入', () => {
  const x = new Float32Array([0, 0.5, -0.5]);
  assert.deepEqual(Array.from(ui.resample(x, 16000, 16000)), Array.from(x));
  const up = ui.resample(sine(8000, 0.5, 200, 0.5), 8000, 16000);
  assert.equal(up.length, 8000);
  assert.ok(peak(up) <= 0.5001 && peak(up) > 0.45);
  assert.equal(ui.resample(new Float32Array(0), 48000, 16000).length, 0);
  assert.equal(ui.resample(null, 48000, 16000).length, 0);
  assert.equal(ui.resample(x, 0, 16000).length, 0);
});

test('WAV 编码：头部各字段、数据长度、能被再次解析', () => {
  const s = new Float32Array([0, 1, -1, 0.5, -0.5, 2, -2, NaN]);
  const buf = ui.encodeWav(s, 16000);
  assert.equal(buf.byteLength, 44 + s.length * 2);
  const w = parseWav(buf);
  assert.equal(w.riffSize, 36 + s.length * 2);
  assert.deepEqual(w.fmt, { format: 1, channels: 1, rate: 16000, byteRate: 32000, align: 2, bits: 16 });
  assert.deepEqual(w.data, [0, 32767, -32768, 16384, -16384, 32767, -32768, 0]);
  const e = parseWav(ui.encodeWav(new Float32Array(0), 16000));
  assert.deepEqual(e.data, []);
  // 一秒的 16 kHz 录音：32044 字节
  assert.equal(ui.encodeWav(new Float32Array(16000), 16000).byteLength, 32044);
});

test('拼接录音分段，并按上限截断', () => {
  const a = new Float32Array([1, 2]), b = new Float32Array([3, 4, 5]);
  assert.deepEqual(Array.from(ui.concatSamples([a, b])), [1, 2, 3, 4, 5]);
  assert.deepEqual(Array.from(ui.concatSamples([a, b], 3)), [1, 2, 3]);
  assert.equal(ui.concatSamples([]).length, 0);
});

test('把文字插入到光标处', () => {
  const ins = ui.insertAtCursor;
  assert.deepEqual(ins('', 0, 0, '你好'), { value: '你好', caret: 2 });
  assert.deepEqual(ins('世界', 0, 0, '你好'), { value: '你好世界', caret: 2 });             // 开头
  assert.deepEqual(ins('请把报告发给我', 3, 3, '季度'), { value: '请把报季度告发给我', caret: 5 });  // 中间
  assert.deepEqual(ins('请把报告', 4, 4, '发给我'), { value: '请把报告发给我', caret: 7 });     // 末尾
  assert.deepEqual(ins('请把报告发给我', 2, 4, '合同'), { value: '请把合同发给我', caret: 4 });   // 替换选区
  assert.deepEqual(ins('请把报告发给我', 4, 2, '合同'), { value: '请把合同发给我', caret: 4 });   // 选区方向反过来
  assert.deepEqual(ins('原有文字', null, null, '新的'), { value: '原有文字新的', caret: 6 });    // 不知道光标：放到末尾
  assert.deepEqual(ins('abc', 99, 99, '你'), { value: 'abc你', caret: 4 });
  // 英文挨着英文补空格，中文不补
  assert.deepEqual(ins('send to', 7, 7, 'Alice'), { value: 'send to Alice', caret: 13 });
  assert.deepEqual(ins('Q3 report', 0, 0, 'Check'), { value: 'Check Q3 report', caret: 6 });
  assert.deepEqual(ins('发给 ', 3, 3, 'Alice'), { value: '发给 Alice', caret: 8 });
  assert.deepEqual(ins('发给', 2, 2, 'Alice'), { value: '发给Alice', caret: 7 });
  assert.equal(ins('a', 1, 1, '<script>x</script>').value, 'a<script>x</script>');   // 只当纯文本处理
});

test('时长格式化', () => {
  assert.equal(ui.formatDuration(0), '0:00');
  assert.equal(ui.formatDuration(5.9), '0:05');
  assert.equal(ui.formatDuration(83), '1:23');
  assert.equal(ui.formatDuration(180), '3:00');
  assert.equal(ui.formatDuration(600), '10:00');
  assert.equal(ui.formatDuration(-3), '0:00');
  assert.equal(ui.formatDuration('x'), '0:00');
});

test('音量指示', () => {
  assert.equal(ui.meterLevel(0), 0);
  assert.equal(ui.meterLevel(0.0001), 0);
  assert.equal(ui.meterLevel(1), 1);
  const mid = ui.meterLevel(0.01);    // -40 dB
  assert.ok(mid > 0.3 && mid < 0.5, String(mid));
  assert.ok(ui.meterLevel(0.1) > mid);
});

test('错误码到提示文字', () => {
  assert.equal(ui.micErrorKind('NotAllowedError'), 'denied');
  assert.equal(ui.micErrorKind('SecurityError'), 'denied');
  assert.equal(ui.micErrorKind('NotFoundError'), 'no-device');
  assert.equal(ui.micErrorKind('NotReadableError'), 'busy');
  assert.equal(ui.micErrorKind('SomethingElse'), 'mic-failed');
  assert.equal(ui.httpErrorKind(409), 'not-configured');
  assert.equal(ui.httpErrorKind(400), 'bad-audio');
  assert.equal(ui.httpErrorKind(502), 'unavailable');
  assert.equal(ui.httpErrorKind(403), 'forbidden');
  assert.equal(ui.httpErrorKind(0), 'offline');
  assert.equal(ui.httpErrorKind(500), 'failed');
  const denied = ui.voiceError('denied');
  assert.match(denied.text, /地址栏/);
  assert.match(denied.text, /允许/);
  assert.equal(denied.settings, false);
  const nc = ui.voiceError('not-configured', '语音输入还没有开启。请到「设置 → 语音识别」里选择识别方式。');
  assert.equal(nc.settings, true);
  assert.match(nc.text, /设置/);
  assert.equal(ui.voiceError('not-configured').settings, true);
  assert.match(ui.voiceError('unavailable', '连不上识别服务 http://10.0.0.1：timed out').text, /暂时连不上/);   // 不把技术细节给使用者
  assert.match(ui.voiceError('bad-audio', '录音太长（200 秒），一次最多 180 秒，请分几段说。').text, /太长/);
  assert.match(ui.voiceError('silent').text, /没有听到声音/);
  assert.match(ui.voiceError('empty').text, /没有识别出文字/);
  assert.match(ui.voiceError('unsupported').text, /浏览器/);
  assert.match(ui.voiceError('???').text, /没有成功/);
  for (const k of ['unsupported', 'denied', 'no-device', 'busy', 'silent', 'not-configured', 'unavailable', 'timeout', 'empty', 'offline']) {
    assert.ok(!/[A-Za-z]{4,}/.test(ui.voiceError(k).text.replace(/Chrome|Edge|Safari|Agent/g, '')), '提示应该是中文：' + k);
  }
});

test('语音快捷键和内核名', () => {
  assert.equal(ui.isVoiceShortcut({ ctrlKey: true, shiftKey: true, key: 'M', code: 'KeyM' }), true);
  assert.equal(ui.isVoiceShortcut({ metaKey: true, shiftKey: true, key: 'm' }), true);
  assert.equal(ui.isVoiceShortcut({ ctrlKey: true, key: 'm' }), false);
  assert.equal(ui.isVoiceShortcut({ ctrlKey: true, shiftKey: true, altKey: true, key: 'M' }), false);
  assert.equal(ui.isVoiceShortcut({ shiftKey: true, key: 'M' }), false);
  assert.equal(ui.kernelLabel('langgraph'), 'LangChain / LangGraph');
  assert.equal(ui.kernelLabel('builtin'), '过渡内核');
  assert.equal(ui.kernelLabel(undefined), '过渡内核');
});

test('index.html 里的元素 id 和 app.js 对得上', () => {
  const fs = require('fs');
  const html = fs.readFileSync(path.join(here, '..', 'ui', 'index.html'), 'utf8');
  const js = fs.readFileSync(path.join(here, '..', 'ui', 'app.js'), 'utf8');
  const ids = new Set([...html.matchAll(/\sid="([^"]+)"/g)].map(m => m[1]));
  const dynamic = new Set(['onboard-cancel']);   // 由脚本动态创建
  const used = new Set([...js.matchAll(/\$\('([a-z][a-z0-9-]*)'\)/g), ...js.matchAll(/\bon\('([a-z][a-z0-9-]*)'/g)].map(m => m[1]));
  for (const id of ['home-mic', 'home-voice', 'composer-mic', 'composer-voice']) assert.ok(ids.has(id), '缺少 #' + id);
  const missing = [...used].filter(id => !ids.has(id) && !dynamic.has(id));
  assert.deepEqual(missing, []);
});

console.log('聊天为主的界面');

// 造一轮：用户一句话 → 两步工具（第二步还在跑）
function turnView() {
  seq = 0;
  const v = ui.newView({ id: 's1', status: 'idle' });
  ui.reduceEvent(v, ev('user_message', { text: '看看报表' }));
  ui.reduceEvent(v, ev('status', { status: 'running' }));
  ui.reduceEvent(v, ev('assistant_message', { content: '', tool_calls: [
    { id: 'c1', name: 'list_dir', arguments: { path: '.' } },
    { id: 'c2', name: 'read_file', arguments: { path: '报表.xlsx' } }] }));
  ui.reduceEvent(v, ev('tool_started', { action_id: 'a1', call_id: 'c1', tool: 'list_dir', args: { path: '.' } }));
  ui.reduceEvent(v, ev('tool_result', { call_id: 'c1', action_id: 'a1', tool: 'list_dir', ok: true, content: '[]' }));
  ui.reduceEvent(v, ev('tool_started', { action_id: 'a2', call_id: 'c2', tool: 'read_file', args: { path: '报表.xlsx' } }));
  return v;
}
const stepsOf = (v) => ui.groupTimeline(v.items, v.status).filter(e => e.type === 'steps');

test('步骤折叠：运行中显示当前这一步', () => {
  const v = turnView();
  const g = stepsOf(v);
  assert.equal(g.length, 1, '一轮只有一行');
  assert.equal(g[0].fold.text, '正在：读取文件 报表.xlsx…');
  assert.equal(g[0].fold.running, true);
  assert.equal(g[0].fold.count, 2);
  assert.equal(g[0].fold.current, v.items.find(i => i.callId === 'c2').key);
});

test('步骤折叠：做完显示"做了 N 步"，出错要提示', () => {
  const v = turnView();
  ui.reduceEvent(v, ev('tool_result', { call_id: 'c2', action_id: 'a2', tool: 'read_file', ok: false, content: '文件不存在' }));
  ui.reduceEvent(v, ev('assistant_message', { content: '没找到报表。', tool_calls: [] }));
  ui.reduceEvent(v, ev('status', { status: 'done' }));
  const f = stepsOf(v)[0].fold;
  assert.equal(f.running, false);
  assert.equal(f.failed, 1);
  assert.equal(f.text, '做了 2 步，其中 1 步出错');
  const r = ui.foldSteps([{ kind: 'tool', tool: 'read_file', args: { path: 'a' }, state: 'failed', key: 'k1' },
    { kind: 'tool', tool: 'read_file', args: { path: 'b' }, state: 'running', key: 'k2' }], { live: true });
  assert.equal(r.text, '正在：读取文件 b…');
  assert.equal(r.note, '有 1 步出错');
  assert.equal(ui.foldSteps([], {}).text, '做了 0 步');
});

test('步骤折叠：被批准卡片打断时卡片不折进去', () => {
  const v = turnView();
  ui.reduceEvent(v, ev('tool_result', { call_id: 'c2', action_id: 'a2', tool: 'read_file', ok: true, content: 'x' }));
  ui.reduceEvent(v, ev('assistant_message', { content: '', tool_calls: [{ id: 'c3', name: 'write_file', arguments: { path: '汇总.md', content: 'x' } }] }));
  ui.reduceEvent(v, ev('approval_requested', { action_id: 'a3', call_id: 'c3', tool: 'write_file', args: { path: '汇总.md' }, risk: 'overwrite' }));
  ui.reduceEvent(v, ev('status', { status: 'waiting_approval' }));
  const out = ui.groupTimeline(v.items, v.status);
  const g = out.filter(e => e.type === 'steps');
  assert.equal(g.length, 1);
  assert.equal(g[0].steps.length, 2, '等批准的那一步不算进折叠');
  assert.equal(g[0].fold.text, '做了 2 步，等你批准');
  assert.equal(g[0].fold.waiting, 'approval');
  const card = out.find(e => e.type === 'item' && e.item.kind === 'tool');
  assert.ok(card, '批准卡片单独显示');
  assert.equal(card.item.state, 'awaiting');
  ui.reduceEvent(v, ev('approval_decided', { action_id: 'a3', allow: true }));
  ui.reduceEvent(v, ev('status', { status: 'running' }));
  assert.equal(stepsOf(v)[0].steps.length, 3, '批准之后这一步回到折叠行里');
  assert.ok(!ui.groupTimeline(v.items, v.status).some(e => e.type === 'item' && e.item.kind === 'tool'));
});

test('步骤折叠：提问卡片单独显示，折叠行写"等你回答"', () => {
  const v = turnView();
  ui.reduceEvent(v, ev('tool_result', { call_id: 'c2', action_id: 'a2', tool: 'read_file', ok: true, content: 'x' }));
  ui.reduceEvent(v, ev('assistant_message', { content: '', tool_calls: [{ id: 'q1', name: 'ask_user', arguments: { question: '要哪个月？', options: ['9 月', '10 月'] } }] }));
  ui.reduceEvent(v, ev('question', { call_id: 'q1', question: '要哪个月？', options: ['9 月', '10 月'] }));
  ui.reduceEvent(v, ev('status', { status: 'waiting_input' }));
  const out = ui.groupTimeline(v.items, v.status);
  assert.ok(out.some(e => e.type === 'item' && e.item.kind === 'question'), '提问卡片在对话流里');
  assert.equal(out.find(e => e.type === 'steps').fold.text, '做了 2 步，等你回答');
});

test('步骤折叠：重连补发的重复事件不让步数算错', () => {
  const v = turnView();
  ui.reduceEvent(v, { seq: 5, session_id: 's1', type: 'tool_started', payload: { action_id: 'a1', call_id: 'c1', tool: 'list_dir', args: {} } });
  ui.reduceEvent(v, { seq: 3, session_id: 's1', type: 'assistant_message', payload: { content: '', tool_calls: [{ id: 'c9', name: 'read_file', arguments: {} }] } });
  assert.equal(stepsOf(v)[0].fold.count, 2);
});

test('一轮一行：多轮各自折叠，key 稳定（展开状态跟着 key 走）', () => {
  const v = turnView();
  ui.reduceEvent(v, ev('tool_result', { call_id: 'c2', action_id: 'a2', tool: 'read_file', ok: true, content: 'x' }));
  ui.reduceEvent(v, ev('status', { status: 'done' }));
  const k1 = stepsOf(v)[0].key;
  ui.reduceEvent(v, ev('user_message', { text: '再查一个' }));
  ui.reduceEvent(v, ev('status', { status: 'running' }));
  ui.reduceEvent(v, ev('assistant_message', { content: '', tool_calls: [{ id: 'c5', name: 'read_file', arguments: { path: 'b.xlsx' } }] }));
  const g = stepsOf(v);
  assert.equal(g.length, 2);
  assert.equal(g[0].key, k1, '前一轮的 key 不因为后面的事件而变');
  assert.equal(g[0].fold.running, false, '只有最后一轮算运行中');
  assert.equal(g[1].fold.text, '正在：读取文件 b.xlsx…');
  const open = {};
  const s0 = ui.stepsSig(g[1], open);
  open[g[1].key] = true;
  assert.notEqual(ui.stepsSig(g[1], open), s0, '展开了要重画');
  const s1 = ui.stepsSig(g[1], open);
  ui.reduceEvent(v, ev('tool_started', { action_id: 'a5', call_id: 'c5', tool: 'read_file', args: {} }));
  assert.notEqual(ui.stepsSig(stepsOf(v)[1], open), s1, '步骤变了要重画');
  assert.equal(ui.stepsSig(stepsOf(v)[1], open), ui.stepsSig(stepsOf(v)[1], open), '没变就不重画');
});

test('成品收成文件卡片，放在这一轮最后，同一个文件只留最新版本', () => {
  seq = 0;
  const v = ui.newView({ id: 's1' });
  ui.reduceEvent(v, ev('user_message', { text: '做个周报' }));
  ui.reduceEvent(v, ev('artifact', { id: 'f1', path: '周报.docx', version: 1 }));
  ui.reduceEvent(v, ev('artifact', { id: 'f2', path: '附表.xlsx', version: 1 }));
  ui.reduceEvent(v, ev('artifact', { id: 'f3', path: '周报.docx', version: 2 }));
  ui.reduceEvent(v, ev('assistant_message', { content: '做好了。', tool_calls: [] }));
  ui.reduceEvent(v, ev('status', { status: 'done' }));
  const out = ui.groupTimeline(v.items, v.status);
  assert.deepEqual(out.map(e => e.type === 'item' ? e.item.kind : e.type), ['user', 'assistant', 'files']);
  assert.deepEqual(out[2].files.map(f => [f.path, f.version]), [['周报.docx', 2], ['附表.xlsx', 1]]);
  assert.equal(ui.filesSig(out[2].files), '周报.docx@2|附表.xlsx@1');
  assert.deepEqual(ui.groupTimeline(null, 'idle'), []);
});

test('带 files 的用户发言：归并成附件列表', () => {
  seq = 0;
  const v = ui.newView({ id: 's1' });
  ui.reduceEvent(v, ev('user_message', { text: '', files: ['上传/报表.xlsx', '上传/说明.pdf', '上传/报表.xlsx', '', 3] }));
  const u = v.items[0];
  assert.equal(u.kind, 'user');
  assert.deepEqual(u.files.map(f => f.name), ['报表.xlsx', '说明.pdf'], '去重，去掉空的和不是文字的');
  assert.equal(u.files[0].path, '上传/报表.xlsx');
  assert.equal(u.files[0].size, null);
  assert.equal(v.title, '', '只有附件时标题留给后端');
  ui.reduceEvent(v, ev('user_message', { text: '没附件' }));
  assert.deepEqual(v.items[1].files, []);
  ui.reduceEvent(v, ev('user_message', { text: '坏的', files: 'x' }));
  assert.deepEqual(v.items[2].files, []);
});

test('附件列表的归一化和大小的显示', () => {
  const r = ui.normalizeFiles(['a\\b\\c.xlsx', { path: '上传/d.pdf', name: 'D 报告.pdf', size: '2048' }, { path: '上传/e.txt', size: 'abc' }, null, { path: '  ' }]);
  assert.deepEqual(r, [
    { path: 'a/b/c.xlsx', name: 'c.xlsx', size: null },
    { path: '上传/d.pdf', name: 'D 报告.pdf', size: 2048 },
    { path: '上传/e.txt', name: 'e.txt', size: null }]);
  assert.deepEqual(ui.normalizeFiles(undefined), []);
  assert.match(ui.formatSize(12345), /KB/);
  assert.match(ui.formatSize(5 * 1024 * 1024), /MB/);
  const st = ui.attachState([{ state: 'done', path: '上传/a.xlsx' }, { state: 'done', path: '上传/a.xlsx' },
    { state: 'uploading' }, { state: 'error' }, null]);
  assert.deepEqual(st, { paths: ['上传/a.xlsx'], uploading: 1, failed: 1 });
  assert.equal(ui.uploadLimit({ upload_max_mb: 10 }), 10 * 1024 * 1024);
  assert.equal(ui.uploadLimit({}), 50 * 1024 * 1024);
  assert.equal(ui.uploadLimit({ upload_max_mb: 'x' }), 50 * 1024 * 1024);
  assert.equal(ui.tooBigText('大表.xlsx', 50 * 1024 * 1024), '「大表.xlsx」太大了，最多 50 MB。');
});

test('图片和其他文件分开；只在拖的是文件时提示', () => {
  const r = ui.splitFiles([{ name: 'a.png', type: 'image/png' }, { name: 'b.xlsx', type: '' }, { name: 'c.svg', type: 'image/svg+xml' }, null]);
  assert.deepEqual(r.images.map(f => f.name), ['a.png']);
  assert.deepEqual(r.others.map(f => f.name), ['b.xlsx', 'c.svg'], 'SVG 不当图片给模型看');
  assert.equal(ui.isFileDrag({ types: ['text/plain'] }), false);
  assert.equal(ui.isFileDrag({ types: ['Files', 'text/uri-list'] }), true);
  assert.equal(ui.isFileDrag({ types: { length: 0, contains: (t) => t === 'Files' } }), true);
  assert.equal(ui.isFileDrag(null), false);
});

test('上传：地址和出错的话', () => {
  assert.equal(ui.uploadUrl('w 1', '报表 1.xlsx'), '/api/upload?workspace=w%201&name=' + encodeURIComponent('报表 1.xlsx'));
  assert.equal(ui.uploadErrorText(404, { error: '没有这个接口' }), ui.UPLOAD_MISSING, '旧版后端');
  assert.equal(ui.uploadErrorText(404, null), ui.UPLOAD_MISSING);
  assert.equal(ui.uploadErrorText(405, null), ui.UPLOAD_MISSING);
  assert.match(ui.uploadErrorText(404, { error: '没有这个工作区' }), /不在列表里/);
  assert.equal(ui.uploadErrorText(413, { error: '文件太大：上限是 50 MB。' }), '文件太大：上限是 50 MB。');
  assert.match(ui.uploadErrorText(413, null), /太大/);
  assert.equal(ui.uploadErrorText(400, { error: '文件名不能为空，也不能只有点' }), '文件名不能为空，也不能只有点');
});

test('发送按钮：运行中、等批准时是停止；等回答、已暂停、有内容时是发送', () => {
  assert.equal(ui.sendMode('running', false), 'stop');
  assert.equal(ui.sendMode('waiting_approval', false), 'stop');
  assert.equal(ui.sendMode('running', true), 'send', '运行中补充要求');
  assert.equal(ui.sendMode('waiting_input', false), 'send', '等回答时发消息就是回答');
  assert.equal(ui.sendMode('paused', false), 'send');
  assert.equal(ui.sendMode('done', false), 'send');
  assert.equal(ui.sendMode('idle', false), 'send');
});

test('文件夹小节：不含默认空间，按名字排，标出当前的', () => {
  const list = [
    { id: 'd', name: '默认', path: '/u/CompanyAgent', is_default: true },
    { id: 'w2', name: 'B 销售', path: '/u/销售' },
    { id: 'w1', name: 'A 采购', path: '/u/采购' },
    { id: 'w3', name: '', path: '/u/C 经营' }];
  const r = ui.folderList(list, 'd', 'w2');
  assert.deepEqual(r.map(f => f.id), ['w1', 'w2', 'w3']);
  assert.deepEqual(r.filter(f => f.current).map(f => f.id), ['w2']);
  assert.equal(r.find(f => f.id === 'w3').name, 'C 经营', '没名字时用文件夹名');
  assert.equal(ui.folderList(list.slice(1), null, 'w1').length, 3, '旧版后端：没有默认空间，全部列出');
  assert.deepEqual(ui.folderList(null, null, null), []);
});

test('旧版后端的降级判断', () => {
  const ws = [{ id: 'a' }, { id: 'b', is_default: true }];
  assert.equal(ui.defaultWorkspaceId({ default_workspace_id: 'b' }, ws), 'b');
  assert.equal(ui.defaultWorkspaceId({}, ws), 'b', '只有 is_default 也认');
  assert.equal(ui.defaultWorkspaceId({ default_workspace_id: 'zzz' }, [{ id: 'a' }]), null, '编号对不上当没有');
  assert.equal(ui.defaultWorkspaceId({ default_workspace_id: null }, [{ id: 'a' }]), null);
  assert.equal(ui.startWorkspace({ default_workspace_id: 'b' }, ws, 'a'), 'b', '有默认空间就从它开始');
  assert.equal(ui.startWorkspace({}, [{ id: 'a' }, { id: 'c' }], 'c'), 'c', '旧版：上次用过的');
  assert.equal(ui.startWorkspace({}, [{ id: 'a' }], 'gone'), 'a');
  assert.equal(ui.startWorkspace({}, [], ''), null);
  assert.equal(ui.needsOnboard([], null), true, '旧版且一个文件夹都没有：先选文件夹');
  assert.equal(ui.needsOnboard([{ id: 'a' }], null), false);
  assert.equal(ui.needsOnboard([{ id: 'b' }], 'b'), false);
});

test('同一个文件夹不重复连接', () => {
  assert.equal(ui.samePath('/Users/zhang/报表/', '/Users/zhang/报表'), true);
  assert.equal(ui.samePath('D:\\工作\\经营', 'd:/工作/经营/'), true);
  assert.equal(ui.samePath('/a/b', '/a/B'), false);
  assert.equal(ui.samePath('', ''), false);
  assert.equal(ui.samePath('/', '/'), true);
});

test('新对话页的建议：每个岗位轮流取，最多 4 个', () => {
  const groups = [{ role: '财务', items: [{ title: 'f1' }, { title: 'f2' }, { title: 'f3' }] }, { role: '销售', items: [{ title: 's1' }] }];
  assert.deepEqual(ui.pickSuggestions(groups, 4).map(t => t.title), ['f1', 's1', 'f2', 'f3']);
  assert.deepEqual(ui.pickSuggestions(groups, 2).map(t => t.title), ['f1', 's1']);
  assert.deepEqual(ui.pickSuggestions([], 4), []);
});

test('界面上不再出现"任务""工作文件夹"', () => {
  const fs = require('fs');
  const html = fs.readFileSync(path.join(here, '..', 'ui', 'index.html'), 'utf8').replace(/<!--[\s\S]*?-->/g, '');
  assert.ok(!/工作文件夹/.test(html), 'index.html 里还有"工作文件夹"');
  assert.ok(!/任务/.test(html), 'index.html 里还有"任务"');
  const js = fs.readFileSync(path.join(here, '..', 'ui', 'app.js'), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '');
  const strs = [...js.matchAll(/'([^'\\\n]*(?:\\.[^'\\\n]*)*)'/g)].map(m => m[1]);
  const bad = strs.filter(s => /工作文件夹|未命名任务|新建任务|这个任务|任务已|任务记录|任务编号/.test(s));
  assert.deepEqual(bad, []);
});

console.log('斜杠指令');

const SKILLS = [{ name: 'slides', description: '把数据做成幻灯片', source: 'user' }, { name: 'new', description: '和内置重名' },
  { name: 'has space', description: 'x' }];
const TPLS = ui.groupTemplates([{ role: '财务', title: '月度 对账', prompt: '核对本月的银行流水和账面' }]);

test('parseSlash：/ ／ 、 开头都算，拆出名字和参数', () => {
  assert.deepEqual(ui.parseSlash('/new'), { name: 'new', arg: '', spaced: false });
  assert.deepEqual(ui.parseSlash('／改名 季度 汇报 '), { name: '改名', arg: '季度 汇报', spaced: true });
  assert.deepEqual(ui.parseSlash('、记住 我在上海'), { name: '记住', arg: '我在上海', spaced: true });
  assert.deepEqual(ui.parseSlash('/'), { name: '', arg: '', spaced: false });
  assert.deepEqual(ui.parseSlash('/整理 '), { name: '整理', arg: '', spaced: true });
  assert.deepEqual(ui.parseSlash('/slides\n第二行'), { name: 'slides', arg: '第二行', spaced: true });
  for (const v of ['new', ' /new', '', null, '\\new', '。new']) assert.equal(ui.parseSlash(v), null, String(v));
});

test('按状态过滤：没打开对话时不出现停止、继续、整理、改名、导出、删除、存成技能', () => {
  const names = ctx => ui.slashCommands(ctx, [], []).map(c => c.id);
  const home = names({ session: false, status: 'idle' });
  for (const id of ['stop', 'continue', 'compact', 'rename', 'export', 'delete', 'save-skill']) assert.ok(!home.includes(id), id);
  for (const id of ['new', 'remember', 'memory', 'theme', 'status', 'help', 'mode', 'search']) assert.ok(home.includes(id), id);
  const idle = names({ session: true, status: 'done' });
  assert.ok(idle.includes('rename') && idle.includes('compact') && !idle.includes('stop') && !idle.includes('continue'));
  const run = names({ session: true, status: 'running' });
  assert.ok(run.includes('stop') && !run.includes('continue'));
  assert.ok(names({ session: true, status: 'waiting_approval' }).includes('stop'));
  const paused = names({ session: true, status: 'paused' });
  assert.ok(paused.includes('continue') && !paused.includes('stop'));
});

test('每个内置指令都有中文名、英文别名和说明，名字不重复', () => {
  const seen = new Set();
  for (const c of ui.SLASH_COMMANDS) {
    assert.ok(c.name && c.alias && c.desc, JSON.stringify(c));
    for (const k of [c.name, c.alias]) { assert.ok(!seen.has(k), '重复：' + k); seen.add(k); }
  }
  assert.equal(ui.SLASH_COMMANDS.length, 24);
});

test('技能和模板也是指令；和内置重名、带空格的技能不做成指令', () => {
  const list = ui.slashCommands({ session: false }, SKILLS, TPLS);
  const sk = list.filter(c => c.group === 'skill');
  assert.deepEqual(sk.map(c => c.name), ['slides']);
  assert.equal(sk[0].desc, '把数据做成幻灯片');
  const tp = list.filter(c => c.group === 'tpl');
  assert.equal(tp.length, 1);
  assert.equal(tp[0].name, '月度对账');
  assert.equal(tp[0].prompt, '核对本月的银行流水和账面');
  assert.equal(ui.findSlash(list, 'new').group, 'cmd');
});

test('筛选：中文名、英文别名、说明都能匹配，大小写不敏感，组的顺序是指令、技能、模板', () => {
  const list = ui.slashCommands({ session: true, status: 'idle' }, SKILLS, TPLS);
  assert.equal(ui.filterSlash(list, '改名')[0].id, 'rename');
  assert.equal(ui.filterSlash(list, 'REN')[0].id, 'rename');
  assert.equal(ui.filterSlash(list, 'comp')[0].id, 'compact');
  assert.ok(ui.filterSlash(list, '幻灯片').some(c => c.name === 'slides'));     // 说明里的字
  assert.ok(ui.filterSlash(list, '上下文').some(c => c.id === 'compact'));
  assert.ok(ui.filterSlash(list, '对账').some(c => c.group === 'tpl'));
  const all = ui.filterSlash(list, '');
  assert.equal(all.length, list.length);
  const groups = all.map(c => c.group);
  assert.deepEqual(groups, [...groups].sort((a, b) => ['cmd', 'skill', 'tpl'].indexOf(a) - ['cmd', 'skill', 'tpl'].indexOf(b)));
  assert.deepEqual(ui.filterSlash(list, 'Users/zhang'), []);
  // 完全对上的排在前面
  assert.equal(ui.filterSlash(list, '技能')[0].id, 'skills');
  // 技能名的开头对上了，比内置指令的零散匹配更靠前
  assert.equal(ui.filterSlash(list, 'sl')[0].name, 'slides');
});

test('回车：未知指令当普通消息，路径不当指令', () => {
  const list = ui.slashCommands({ session: false }, SKILLS, TPLS);
  assert.deepEqual(ui.resolveSlash('/Users/zhang/a.xlsx 看一下', list), { kind: 'unknown' });
  assert.deepEqual(ui.resolveSlash('/xyz', list), { kind: 'unknown' });
  assert.equal(ui.resolveSlash('你好', list), null);
  assert.equal(ui.resolveSlash('', list), null);
});

test('回车：现在用不了的、缺参数的、可以执行的', () => {
  const list = ui.slashCommands({ session: false }, SKILLS, TPLS);
  const r1 = ui.resolveSlash('/停止', list);
  assert.equal(r1.kind, 'unavailable'); assert.equal(r1.cmd.id, 'stop');
  assert.equal(ui.resolveSlash('/记住', list).kind, 'need-arg');
  assert.equal(ui.resolveSlash('/remember   ', list).kind, 'need-arg');
  const r2 = ui.resolveSlash('／记住 我喜欢用万元做单位', list);
  assert.equal(r2.kind, 'run'); assert.equal(r2.cmd.id, 'remember'); assert.equal(r2.arg, '我喜欢用万元做单位');
  const r3 = ui.resolveSlash('、MEMORY', list);
  assert.equal(r3.kind, 'run'); assert.equal(r3.cmd.id, 'memory');
  const s = ui.slashCommands({ session: true, status: 'idle' }, [], []);
  const r4 = ui.resolveSlash('/整理 保留预算数字', s);
  assert.equal(r4.cmd.id, 'compact'); assert.equal(r4.arg, '保留预算数字');
  assert.equal(ui.resolveSlash('/compact', s).kind, 'run');      // 参数可以不写
  const r5 = ui.resolveSlash('/rename 二季度 经营分析', s);
  assert.equal(r5.arg, '二季度 经营分析');
});

test('技能指令：参数是要求，可以为空；生成的请求体带 skill', () => {
  const list = ui.slashCommands({ session: false }, SKILLS, []);
  const r = ui.resolveSlash('/slides 把季度数据做成 5 页', list);
  assert.equal(r.kind, 'run'); assert.equal(r.cmd.skill, 'slides'); assert.equal(r.arg, '把季度数据做成 5 页');
  assert.deepEqual(ui.messageBody(r.arg, { skill: r.cmd.skill }), { text: '把季度数据做成 5 页', skill: 'slides' });
  const r0 = ui.resolveSlash('/slides', list);
  assert.equal(r0.kind, 'run'); assert.equal(r0.arg, '');
  assert.deepEqual(ui.messageBody('', { skill: 'slides', files: ['a.xlsx'] }), { text: '', files: ['a.xlsx'], skill: 'slides' });
  assert.deepEqual(ui.messageBody('你好', { images: [], files: [] }), { text: '你好' });
});

test('确认后放进输入框的文字：带参数的放名字，模板放模板文字，不带参数的直接执行', () => {
  const list = ui.slashCommands({ session: true, status: 'idle' }, SKILLS, TPLS);
  assert.equal(ui.slashFill(ui.findSlash(list, 'rename')), '/改名 ');
  assert.equal(ui.slashFill(ui.findSlash(list, 'slides')), '/slides ');
  assert.equal(ui.slashFill(ui.findSlash(list, '月度对账')), '核对本月的银行流水和账面');
  assert.equal(ui.slashFill(ui.findSlash(list, 'memory')), null);
  assert.equal(ui.slashFill(ui.findSlash(list, '主题')), '/主题 ');
});

test('user_message 事件里的 skill 留在气泡上', () => {
  const v = ui.newView({ id: 's1' });
  ui.reduceEvent(v, { seq: 1, type: 'user_message', payload: { text: '做 5 页', skill: 'slides' } });
  ui.reduceEvent(v, { seq: 2, type: 'user_message', payload: { text: '普通' } });
  assert.equal(v.items[0].skill, 'slides');
  assert.equal(v.items[1].skill, '');
});

test('批准方式和主题按名字或 id 匹配', () => {
  const modes = [{ id: 'manual', label: '每步都问' }, { id: 'auto', label: '自动' }, { id: 'full', label: '全放行' }];
  assert.equal(ui.matchMode(modes, '自动').id, 'auto');
  assert.equal(ui.matchMode(modes, 'FULL').id, 'full');
  assert.equal(ui.matchMode(modes, '每步').id, 'manual');
  assert.equal(ui.matchMode(modes, '随便'), null);
  const themes = [{ id: 'plain', name: '素白' }, { id: 'ink-night', name: '墨夜' }, { id: 'solarized-light', name: 'Solarized Light' },
    { id: 'catppuccin-latte', name: 'Catppuccin Latte' }, { id: 'catppuccin-mocha', name: 'Catppuccin Mocha' }];
  assert.equal(ui.matchTheme(themes, '墨夜'), 'ink-night');
  assert.equal(ui.matchTheme(themes, 'ink-night'), 'ink-night');
  assert.equal(ui.matchTheme(themes, 'solarized light'), 'solarized-light');
  assert.equal(ui.matchTheme(themes, 'Mocha'), 'catppuccin-mocha');
  assert.equal(ui.matchTheme(themes, 'catppuccin'), null);           // 两个都像，不猜
  assert.equal(ui.matchTheme(themes, '跟随系统'), 'auto');
  assert.equal(ui.matchTheme(themes, ''), null);
  assert.equal(ui.matchTheme(undefined, '素白'), null);
});

test('/整理 只在对话没在做事时出现', () => {
  const ids = st => ui.slashCommands({ session: true, status: st }, [], []).map(c => c.id);
  assert.ok(ids('done').includes('compact'));
  assert.ok(ids('paused').includes('compact'));
  assert.ok(!ids('running').includes('compact'));
  assert.ok(!ids('waiting_input').includes('compact'));
});

test('compact 事件：started 标出正在整理，done 收掉，failed 收掉并留一行出错', () => {
  const v = ui.newView({ id: 's1' });
  ui.reduceEvent(v, { seq: null, type: 'compact', payload: { state: 'started', focus: '预算' } });
  assert.equal(v.compacting, true);
  ui.reduceEvent(v, { seq: 5, type: 'summary', payload: { text: '摘要', upto_seq: 4 } });
  ui.reduceEvent(v, { seq: 6, type: 'compact', payload: { state: 'done', compacted: true } });
  assert.equal(v.compacting, false);
  assert.deepEqual(v.items.map(i => i.kind), ['summary']);
  ui.reduceEvent(v, { seq: null, type: 'compact', payload: { state: 'started' } });
  ui.reduceEvent(v, { seq: 7, type: 'compact', payload: { state: 'failed', error: '整理失败：模型没回' } });
  assert.equal(v.compacting, false);
  assert.equal(v.items[1].tone, 'error'); assert.equal(v.items[1].text, '整理失败：模型没回');
  assert.doesNotThrow(() => { ui.reduceEvent(v, { seq: 8, type: 'compact' }); ui.reduceEvent(v, { seq: 9, type: 'compact', payload: 'x' }); });
});

test('/整理 的两种返回都给一句话', () => {
  assert.equal(ui.compactText({ ok: true, compacted: true }), '已整理较早的内容。');
  assert.equal(ui.compactText({ ok: true, compacted: false }), '现在没有需要整理的。');
  assert.match(ui.compactText({ ok: true, started: true }), /开始整理/);
});

test('/状态 卡片的行：数字带千位分隔和百分比；没打开对话时没有上下文和步数', () => {
  const st = { model: 'qwen3', kernel: 'langgraph', context: { used: 12345, limit: 128000 }, steps: 7,
    memory: { user: { used: 800, limit: 2000 }, workspace: { used: 0, limit: 2000 } }, approval_mode: 'auto' };
  const rows = ui.statusRows(st, { kernel_notice: null }, id => id === 'auto' ? '自动' : id);
  const m = Object.fromEntries(rows.map(r => [r.label, r.value]));
  assert.equal(m['模型'], 'qwen3');
  assert.equal(m['内核'], 'LangGraph');
  assert.equal(m['上下文'], '12,345 / 128,000（10%）');
  assert.equal(m['已做步数'], '7');
  assert.equal(m['关于我'], '800 / 2,000（40%）');
  assert.equal(m['批准方式'], '自动');
  const home = ui.statusRows({ memory: { user: { used: 10, budget: 100, pct: 10 } } }, { kernel: 'builtin', model: 'm1', kernel_notice: '没装包' });
  const hm = Object.fromEntries(home.map(r => [r.label, r.value]));
  assert.equal(hm['模型'], 'm1'); assert.equal(hm['内核'], '过渡内核');
  assert.ok(!('上下文' in hm) && !('已做步数' in hm));
  assert.equal(hm['关于我'], '10 / 100（10%）');
  // 给开发者看的原话不进卡片：过渡内核时只给一行小字，指到设置 → 高级
  assert.ok(!('说明' in hm));
  assert.ok(!home.some(r => /没装包|pip/.test(r.value + (r.hint || ''))));
  assert.equal(home.find(r => r.label === '内核').hint, '详情见 设置 → 高级');
  assert.equal(rows.find(r => r.label === '内核').value, 'LangGraph');
  assert.ok(!rows.find(r => r.label === '内核').hint);
  assert.doesNotThrow(() => ui.statusRows(null, null));
  assert.equal(ui.groupNum(1234567), '1,234,567');
});

test('拼音也能找到内置指令：全拼和首字母，排在中文名和英文别名之后', () => {
  const list = ui.slashCommands({ session: true, status: 'idle' }, SKILLS, TPLS);
  const ids = q => ui.filterSlash(list, q).filter(c => c.group === 'cmd').map(c => c.id);
  assert.ok(ids('ji').includes('remember') && ids('ji').includes('memory'));
  assert.equal(ids('jz')[0], 'remember');
  assert.equal(ids('xdh')[0], 'new');
  assert.equal(ids('zl')[0], 'compact');
  assert.ok(ids('zl').includes('kb'));
  assert.equal(ids('jiyi')[0], 'memory');
  assert.equal(ids('zhuti')[0], 'theme');
  for (const c of ui.SLASH_COMMANDS) assert.ok(c.py && /^[a-z]+ [a-z]+$/.test(c.py), c.id + ' 缺拼音');
  // 中文名和英文别名优先：/sl 还是技能 slides 在最前，/s 开头的英文别名排在拼音前面
  assert.equal(ui.filterSlash(list, 'sl')[0].name, 'slides');
  const st = ui.filterSlash(list, 'st').map(c => c.id);
  assert.ok(st.indexOf('stop') < 0 || st.indexOf('status') < st.indexOf('settings'));
});

test('侧栏按时间分组；每一项右边的时间；步骤用时；问候', () => {
  const now = new Date(2026, 10, 21, 15, 0, 0).getTime();
  const at = (d, h) => new Date(2026, 10, d, h, 5, 0).getTime() / 1000;
  const g = ui.groupSessions([{ id: 'a', updated: at(21, 9) }, { id: 'b', updated: at(20, 22) }, { id: 'c', updated: at(17, 8) },
    { id: 'd', updated: at(2, 8) }, { id: 'e' }], now);
  assert.deepEqual(g.map(x => x.label), ['今天', '昨天', '近 7 天', '更早']);
  assert.deepEqual(g.map(x => x.items.map(s => s.id).join('')), ['a', 'b', 'c', 'de']);
  assert.deepEqual(ui.groupSessions(null, now), []);
  assert.equal(ui.listTime(at(21, 9), now), '09:05');
  assert.equal(ui.listTime(at(20, 22), now), '22:05');
  assert.equal(ui.listTime(at(17, 8), now), '周二');
  assert.equal(ui.listTime(at(2, 8), now), '11月2日');
  assert.equal(ui.listTime(0, now), '');
  assert.equal(ui.formatElapsed(40), '40 秒');
  assert.equal(ui.formatElapsed(72), '1 分 12 秒');
  assert.equal(ui.formatElapsed(120), '2 分');
  assert.equal(ui.formatElapsed(3780), '1 小时 3 分');
  assert.equal(ui.formatElapsed(null), '');
  assert.equal(ui.greeting(9), '上午好');
  assert.equal(ui.greeting(15), '下午好');
  assert.equal(ui.greeting(21), '晚上好');
  assert.equal(ui.greeting(2), '晚上好');
});

test('步骤折叠行带用时：第一步开始到最后一步结束', () => {
  const steps = [
    { key: 'a', kind: 'tool', tool: 'read_file', args: {}, state: 'done', startedAt: 100, endedAt: 110 },
    { key: 'b', kind: 'tool', tool: 'read_file', args: {}, state: 'done', startedAt: 111, endedAt: 172 }];
  assert.equal(ui.foldSteps(steps, {}).secs, 72);
  assert.equal(ui.foldSteps(steps, { live: true }).secs, null);
  assert.equal(ui.foldSteps([{ key: 'a', kind: 'tool', tool: 'read_file', args: {}, state: 'done' }], {}).secs, null);
});

console.log('操控电脑：模型能力、操作网页');

const CAPS_ONE = {
  models: [{ name: 'GX10-A', model: 'qwen3-32b', alive: true, tools: true, vision: false, pointing: false,
             context: 32768, context_detected: 262144, source: { tools: 'probe', vision: 'config', pointing: 'probe' }, probed_at: 1795240000 }],
  model: { tools: true, vision: false, pointing: false, context: 32768, context_detected: 262144, source: {}, probed_at: 1795240000 },
  features: [
    { id: 'browser', name: '操作网页', ok: true, unknown: false, reason_kind: '', reason: '', via: 'GX10-A', fallback: '' },
    { id: 'image_input', name: '看图片附件', ok: false, unknown: false, reason_kind: 'model',
      reason: '当前模型不支持看图，无法使用「看图片附件」。', via: '', fallback: '可以把图里的内容用文字发来，或者上传文档。' },
    { id: 'desktop', name: '操作桌面程序', ok: false, unknown: false, reason_kind: 'component',
      reason: '这台电脑上还没有桌面操控组件，无法使用「操作桌面程序」。', via: '', fallback: '网页上的事可以改用「操作网页」来做。' }
  ]
};
const CAPS_TWO = {
  models: [CAPS_ONE.models[0],
    { name: 'GX10-B', model: 'qwen3-vl-235b', alive: true, tools: true, vision: true, pointing: 'norm1000', context: 262144,
      context_detected: 262144, source: { tools: 'probe', vision: 'probe', pointing: 'probe' }, probed_at: 1795240000 }],
  model: { tools: true, vision: true, pointing: 'norm1000', context: 32768, context_detected: 262144 },
  features: [
    { id: 'browser', name: '操作网页', ok: true, via: 'GX10-A' },
    { id: 'image_input', name: '看图片附件', ok: true, via: 'GX10-B' },
    { id: 'browser_vision', name: '看网页截图', ok: true, unknown: true, via: 'GX10-B' }
  ]
};

test('normalizeCaps：按模型一行，pointing 和来源都留着；坏输入不抛错', () => {
  const c = ui.normalizeCaps(CAPS_TWO);
  assert.equal(c.models.length, 2);
  assert.equal(c.models[1].pointing, 'norm1000');
  assert.equal(c.models[0].pointing, false);
  assert.equal(c.models[0].source.vision, 'config');
  assert.equal(c.byId.image_input.via, 'GX10-B');
  assert.equal(c.byId.browser.fallback, '');
  for (const bad of [null, undefined, 'x', 42, []]) assert.equal(ui.normalizeCaps(bad), null);
  const junk = ui.normalizeCaps({ models: [null, 'x', {}], features: [null, { name: '没有 id' }, { id: 'a' }, { id: 'a', ok: false }] });
  assert.equal(junk.models.length, 1);
  assert.equal(junk.models[0].tools, null);
  assert.equal(junk.models[0].source.tools, 'unknown');
  assert.deepEqual(junk.features.map(f => f.id), ['a']);
  assert.equal(junk.byId.a.ok, true, '重复的 id 只认第一个');
});

test('normalizeCaps：规格 1.4 的老形状（只有 model）也认成一行', () => {
  const c = ui.normalizeCaps({ model: { tools: true, vision: null, context: 32768, source: { tools: 'probe', vision: 'unknown' }, probed_at: null },
    features: [{ id: 'image_input', name: '看图片附件', ok: true, unknown: true }] });
  assert.equal(c.models.length, 1);
  assert.equal(c.models[0].alive, true);
  assert.equal(c.models[0].vision, null);
  assert.equal(c.byId.image_input.unknown, true);
});

test('capsFromError：老后端 404 / 405 → missing；别的错误 → null', () => {
  assert.deepEqual(ui.capsFromError({ status: 404, missing: true }), { missing: true });
  assert.deepEqual(ui.capsFromError({ status: 404 }), { missing: true });
  assert.deepEqual(ui.capsFromError({ status: 405 }), { missing: true });
  assert.equal(ui.capsFromError({ status: 500 }), null);
  assert.equal(ui.capsFromError(new Error('连不上')), null);
});

test('featureGate：用后端原话和"可以改用什么"；老后端和还没取到时什么都不拦', () => {
  const c = ui.normalizeCaps(CAPS_ONE);
  const g = ui.featureGate(c, null, 'image_input');
  assert.equal(g.ok, false);
  assert.equal(g.reason, '当前模型不支持看图，无法使用「看图片附件」。');
  assert.equal(g.fallback, '可以把图里的内容用文字发来，或者上传文档。');
  assert.deepEqual(ui.featureGate(c, null, 'browser'), { ok: true, unknown: false, reason: '', fallback: '', name: '操作网页' });
  // 老后端：不置灰
  assert.equal(ui.featureGate({ missing: true }, { image_input: false }, 'image_input').ok, true);
  assert.equal(ui.featureGate({ missing: true }, undefined, 'image_input').ok, true);
  // 还没取到能力：只信 /api/state 的 features
  assert.equal(ui.featureGate(null, null, 'image_input').ok, true);
  assert.equal(ui.featureGate(null, { image_input: true }, 'image_input').ok, true);
  const off = ui.featureGate(null, { image_input: false }, 'image_input');
  assert.equal(off.ok, false);
  assert.equal(off.reason, '');
  // 能力里没有的功能：当作能用
  assert.equal(ui.featureGate(c, { x: false }, 'nope').ok, true);
  // 未知当作可以试
  const u = ui.featureGate(ui.normalizeCaps(CAPS_TWO), null, 'browser_vision');
  assert.equal(u.ok, true); assert.equal(u.unknown, true);
});

test('contextHint：检测到的比设的大时才提示，数字带千分位', () => {
  assert.equal(ui.contextHint({ context: 32768, context_detected: 262144 }), '模型最多支持 262,144，现在设的是 32,768');
  assert.equal(ui.contextHint({ context: 32768, context_detected: 32768 }), '');
  assert.equal(ui.contextHint({ context: 65536, context_detected: 32768 }), '');
  assert.equal(ui.contextHint({ context: 32768, context_detected: null }), '');
  assert.equal(ui.contextHint({ context: null, context_detected: 262144 }), '');
  assert.equal(ui.contextHint(null), '');
});

test('modelRows：每个模型一行，是 / 否 / 未知 加来源；在线、上下文提示各算各的', () => {
  const rows = ui.modelRows(ui.normalizeCaps(CAPS_TWO));
  assert.equal(rows.length, 2);
  assert.deepEqual(rows[0].cells.map(c => c.value), ['是', '否', '否']);
  assert.deepEqual(rows[0].cells.map(c => c.source), ['检测到的', '配置里写的', '检测到的']);
  assert.deepEqual(rows[1].cells.map(c => c.value), ['是', '是', '是'], 'pointing 是 norm1000 也显示"是"');
  assert.equal(rows[0].context, '32,768');
  assert.equal(rows[0].hint, '模型最多支持 262,144，现在设的是 32,768');
  assert.equal(rows[1].hint, '');
  assert.equal(rows[0].alive, '在线');
  const down = ui.modelRows(ui.normalizeCaps({ models: [{ name: 'B', alive: false, tools: null }] }));
  assert.equal(down[0].alive, '掉线');
  assert.equal(down[0].cells[0].value, '未知');
  assert.equal(down[0].cells[0].source, '还没检测');
  assert.equal(down[0].context, '未知');
  const probed = ui.modelRows(ui.normalizeCaps({ models: [{ name: 'B', tools: null, probed_at: 1 }] }));
  assert.equal(probed[0].cells[0].source, '没检测出来');
  assert.deepEqual(ui.modelRows(null), []);
  assert.deepEqual(ui.modelRows({ missing: true }), []);
});

test('featureLines：可用打勾、由排第一以外的模型承担时写出来；不可用用原话和改用办法', () => {
  const two = ui.featureLines(ui.normalizeCaps(CAPS_TWO));
  assert.equal(two.find(f => f.id === 'browser').via, '', '排第一的模型承担时不写');
  assert.equal(two.find(f => f.id === 'image_input').via, '由 GX10-B 承担');
  assert.equal(two.find(f => f.id === 'browser_vision').unknown, true);
  const one = ui.featureLines(ui.normalizeCaps(CAPS_ONE));
  const img = one.find(f => f.id === 'image_input');
  assert.equal(img.ok, false);
  assert.equal(img.text, '当前模型不支持看图，无法使用「看图片附件」。');
  assert.equal(img.fallback, '可以把图里的内容用文字发来，或者上传文档。');
  assert.deepEqual(ui.featureLines({ missing: true }), []);
  assert.deepEqual(ui.featureLines(null), []);
});

test('normalizeDomain：转小写、去空白和首尾的点；网址只取域名；格式不对给一句话', () => {
  assert.deepEqual(ui.normalizeDomain('  OA.Example.COM '), { ok: true, value: 'oa.example.com', error: '' });
  assert.equal(ui.normalizeDomain('.example.com.').value, 'example.com');
  assert.equal(ui.normalizeDomain('https://OA.example.com:8443/flow?id=1').value, 'oa.example.com');
  assert.equal(ui.normalizeDomain('oa.example.com/path').value, 'oa.example.com');
  assert.equal(ui.normalizeDomain('user@oa.example.com').value, 'oa.example.com');
  assert.equal(ui.normalizeDomain('oa. example .com').value, 'oa.example.com');
  for (const bad of ['', '   ', '...', null]) {
    const r = ui.normalizeDomain(bad);
    assert.equal(r.ok, false); assert.match(r.error, /填一个网站/);
  }
  for (const bad of ['oa_example.com', '内网.com', 'a..b', 'x'.repeat(254), 'a*b.com']) {
    const r = ui.normalizeDomain(bad);
    assert.equal(r.ok, false, bad); assert.match(r.error, /不像网站域名/);
  }
});

test('addDomain：加进名单、重复的不加，原数组不动', () => {
  const list = ['a.com'];
  const r = ui.addDomain(list, ' B.com ');
  assert.deepEqual(r.list, ['a.com', 'b.com']);
  assert.equal(r.error, '');
  assert.deepEqual(list, ['a.com']);
  const dup = ui.addDomain(r.list, 'https://B.COM/x');
  assert.match(dup.error, /已经在名单里/);
  assert.deepEqual(dup.list, ['a.com', 'b.com']);
  assert.match(ui.addDomain(null, 'a b_c').error, /不像/);
  assert.deepEqual(ui.addDomain(undefined, 'x.cn').list, ['x.cn']);
});

test('displayOf / stepText：有 display 时用它的标题和图标，没有时照旧', () => {
  const d = { kind: 'browser', title: '点击按钮「提交审批」', page: '费用报销 - OA', host: 'oa.example.com', text: '' };
  const s = ui.stepText({ tool: 'browser_do', args: { action: 'click', ref: 'e13' }, display: d });
  assert.equal(s.verb, '点击按钮「提交审批」');
  assert.equal(s.icon, 'globe');
  assert.equal(s.where, '费用报销 - OA · oa.example.com');
  assert.equal(s.target, '');
  assert.equal(ui.stepText({ tool: 'desktop_do', display: { kind: 'desktop', title: '点击「保存」' } }).icon, 'monitor');
  // 没有 display、kind 不认识、没有标题：照旧
  const plain = ui.stepText({ tool: 'read_file', args: { path: 'a.md' } });
  assert.deepEqual([plain.verb, plain.target, plain.icon], ['读取文件', 'a.md', '']);
  assert.equal(ui.stepText({ tool: 'read_file', args: {}, display: { kind: 'other', title: 'x' } }).verb, '读取文件');
  assert.equal(ui.displayOf({ kind: 'browser', title: '' }), null);
  assert.equal(ui.displayOf('x'), null);
  assert.equal(ui.stepText({ tool: 'browser_open', args: { url: 'https://oa.example.com' } }).verb, '在浏览器里打开');
  assert.equal(ui.toolInfo('browser_open', { url: 'https://oa.example.com' }).target, 'https://oa.example.com');
});

test('reduceEvent / applyPending：display 从 tool_started、approval_requested、tool_result、pending 里来，后来的页面标题盖掉先前的', () => {
  let v = ui.newView({ id: 's' });
  const d1 = { kind: 'browser', title: '打开 oa.example.com', page: '', host: 'oa.example.com', text: '' };
  ui.reduceEvent(v, { seq: 1, type: 'tool_started', payload: { call_id: 'b1', tool: 'browser_open', args: { url: 'https://oa.example.com' }, display: d1 } });
  ui.reduceEvent(v, { seq: 2, type: 'tool_result', payload: { call_id: 'b1', tool: 'browser_open', ok: true, content: '页面概况…',
    display: { kind: 'browser', title: '打开 oa.example.com', page: '首页 - OA', host: 'oa.example.com' } } });
  const it = v.items[0];
  assert.equal(it.display.page, '首页 - OA');
  assert.equal(it.display.title, '打开 oa.example.com');
  ui.reduceEvent(v, { seq: 3, type: 'approval_requested', payload: { action_id: 'a2', call_id: 'b2', tool: 'browser_do', risk: 'submit',
    args: { action: 'click', ref: 'e13' }, display: { kind: 'browser', title: '点击按钮「提交审批」', page: '费用报销 - OA', host: 'oa.example.com' } } });
  assert.equal(v.items[1].display.title, '点击按钮「提交审批」');
  assert.equal(v.items[1].state, 'awaiting');
  const v2 = ui.applyPending(ui.newView({ id: 's' }), [{ id: 'a9', call_id: 'b9', tool: 'browser_open', risk: 'external', args: { url: 'https://hr.example.com' },
    display: { kind: 'browser', title: '打开 hr.example.com', host: 'hr.example.com' } }]);
  assert.equal(v2.items[0].display.host, 'hr.example.com');
  // 没有 display 的老事件：不多出字段
  ui.reduceEvent(v, { seq: 4, type: 'tool_started', payload: { call_id: 'c1', tool: 'read_file', args: { path: 'x' } } });
  assert.equal(v.items[2].display, undefined);
  assert.equal(ui.mergeDisplay(null, null), null);
});

test('alwaysOption：打开网页给"以后都允许这个网站"；网页上的动作、删除、提交、技能不给；其余照旧', () => {
  const open = ui.alwaysOption({ tool: 'browser_open', risk: 'external', display: { kind: 'browser', title: '打开 hr.example.com', host: 'hr.example.com' } });
  assert.equal(open.label, '以后都允许这个网站');
  assert.match(open.title, /hr\.example\.com/);
  assert.match(ui.alwaysOption({ tool: 'browser_open', risk: 'external', args: { url: 'https://Wiki.Example.com/a' } }).title, /wiki\.example\.com/);
  assert.equal(ui.alwaysOption({ tool: 'browser_do', risk: 'submit', display: { kind: 'browser', title: '点击' } }), null);
  assert.equal(ui.alwaysOption({ tool: 'browser_do', risk: 'web' }), null);
  assert.equal(ui.alwaysOption({ tool: 'desktop_do', risk: 'exec' }), null);
  assert.equal(ui.alwaysOption({ tool: 'delete_file', risk: 'delete' }), null);
  assert.equal(ui.alwaysOption({ tool: 'skill_save', risk: 'skill' }), null);
  assert.equal(ui.alwaysOption({ tool: 'write_file', risk: 'overwrite' }).label, '以后都允许');
});

test('风险级别：web 是"网页操作"，submit 是"提交"', () => {
  assert.equal(ui.riskLabel('web'), '网页操作');
  assert.equal(ui.riskLabel('submit'), '提交');
});

test('statusRows：模型一行写"端点名 · 模型名"；带 browser 时多一行浏览器', () => {
  const rows = ui.statusRows({ model: 'qwen3-vl-235b', endpoint: 'GX10-B', browser: { open: true, url: 'https://oa.example.com/flow', title: '费用报销 - OA' } }, {});
  assert.equal(rows[0].value, 'GX10-B · qwen3-vl-235b');
  const b = rows.find(r => r.label === '浏览器');
  assert.equal(b.value, '开着：费用报销 - OA');
  assert.equal(b.hint, 'https://oa.example.com/flow');
  assert.equal(ui.statusRows({ browser: { open: false } }, {}).find(r => r.label === '浏览器').value, '没开');
  assert.equal(ui.statusRows({}, { model: 'm' }).find(r => r.label === '浏览器'), undefined, '老后端没有 browser 就不显示');
  assert.equal(ui.statusRows({}, { model: 'm' })[0].value, 'm');
});

test('app.js 里只有 setMarkdown 一处给 innerHTML 赋值', () => {
  const fs = require('fs');
  const js = fs.readFileSync(path.join(here, '..', 'ui', 'app.js'), 'utf8');
  const hits = js.match(/\.innerHTML\s*=/g) || [];
  assert.equal(hits.length, 1);
  assert.ok(!/insertAdjacentHTML|outerHTML\s*=|document\.write/.test(js));
});

test('app.css 里没有写死的颜色，深浅全由主题决定', () => {
  const fs = require('fs');
  const css = fs.readFileSync(path.join(here, '..', 'ui', 'app.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  const hex = css.match(/#[0-9a-fA-F]{3,8}\b/g) || [];
  assert.deepEqual(hex, [], '有写死的十六进制颜色');
  assert.ok(!/\b(rgba?|hsla?|hwb|lab|lch|oklab|oklch)\(/i.test(css), '有 rgb() / hsl() 之类的颜色');
  const NAMED = ['white', 'black', 'red', 'green', 'blue', 'gray', 'grey', 'silver', 'yellow', 'orange', 'purple', 'pink', 'navy',
    'teal', 'maroon', 'olive', 'lime', 'aqua', 'fuchsia', 'brown', 'gold', 'beige', 'ivory', 'tan', 'crimson', 'coral',
    'salmon', 'khaki', 'indigo', 'violet', 'cyan', 'magenta', 'whitesmoke', 'gainsboro', 'lightgray', 'darkgray', 'dimgray'];
  // 只看声明的值（冒号后面），不看选择器和变量名
  const values = [...css.matchAll(/:\s*([^;{}]+)[;}]/g)].map(m => m[1].replace(/var\(--[a-z0-9-]+/gi, ''));
  const bad = values.filter(v => new RegExp('(^|[\\s,(])(' + NAMED.join('|') + ')($|[\\s,)])', 'i').test(v));
  assert.deepEqual(bad, [], '有颜色名');
  assert.ok(!/prefers-color-scheme/.test(css), '深浅应该由主题决定，不再跟着 prefers-color-scheme');
  // 用到的变量都在 themes.css 或 app.css 自己的 :root 里定义过
  const themes = fs.readFileSync(path.join(here, '..', 'ui', 'themes.css'), 'utf8');
  const defined = new Set([...(themes + css).matchAll(/(--[a-z0-9-]+)\s*:/gi)].map(m => m[1]));
  const used = new Set([...css.matchAll(/var\((--[a-z0-9-]+)/gi)].map(m => m[1]));
  assert.deepEqual([...used].filter(v => !defined.has(v)), [], '用了没定义的变量');
});

test('index.html 先加载 theme.js 和 themes.css，再加载 app.css；有内联图标，不请求 favicon.ico', () => {
  const fs = require('fs');
  const html = fs.readFileSync(path.join(here, '..', 'ui', 'index.html'), 'utf8');
  const a = html.indexOf('/theme.js'), b = html.indexOf('/themes.css'), c = html.indexOf('/app.css');
  assert.ok(a > 0 && a < b && b < c);
  assert.match(html, /<link rel="icon"[^>]+href="data:image\/svg\+xml/);
  assert.ok(!/回车发送，Shift/.test(html.replace(/title="[^"]*"/g, '')), '"回车发送…"应该只在发送按钮的 title 里');
});


console.log('两种工作模式：CariAi 和 Hspace');

const MODES_INFO = { modes: [
  { id: 'cariai', name: 'CariAi', tagline: '日常助手：动手做事、操作电脑', prompt_line: '想做什么？', placeholder: '问点什么' },
  { id: 'hspace', name: 'Hspace', tagline: '高阶工作空间', prompt_line: '想查什么？', placeholder: '问资料里的事' }] };

test('normalizeModes：认两个以上的模式；老后端没有 modes、坏输入、只有一个都返回 []', () => {
  const m = ui.normalizeModes(MODES_INFO);
  assert.deepEqual(m.map(x => x.id), ['cariai', 'hspace']);
  assert.equal(m[1].name, 'Hspace');
  assert.equal(m[1].prompt_line, '想查什么？');
  assert.deepEqual(ui.normalizeModes({}), []);
  assert.deepEqual(ui.normalizeModes(null), []);
  assert.deepEqual(ui.normalizeModes({ modes: 'x' }), []);
  assert.deepEqual(ui.normalizeModes({ modes: [{ id: 'cariai' }] }), [], '只有一个模式不显示总开关');
  assert.deepEqual(ui.normalizeModes({ modes: [{ id: 'cariai' }, { id: 'cariai' }, null, { id: '<x>' }] }), [], '重复的、坏的 id 不算');
  assert.equal(ui.normalizeModes({ modes: [{ id: 'CariAi' }, { id: 'hspace', name: 'Hspace' }] })[0].name, 'cariai', '没给名字就用 id');
});

test('pickMode：地址里的优先，其次上次记住的，都不行用第一个；老后端是空', () => {
  const m = ui.normalizeModes(MODES_INFO);
  assert.equal(ui.pickMode(m, 'hspace', 'cariai'), 'hspace');
  assert.equal(ui.pickMode(m, '', 'hspace'), 'hspace');
  assert.equal(ui.pickMode(m, 'nope', 'bad'), 'cariai');
  assert.equal(ui.pickMode([], 'hspace', 'hspace'), '');
});

test('sessionMode / sessionsForMode：没有 mode 的老对话算 CariAi；老后端不筛', () => {
  const list = [{ id: 'a' }, { id: 'b', mode: 'hspace' }, { id: 'c', mode: 'cariai' }, { id: 'd', mode: 'HSPACE' }];
  assert.equal(ui.sessionMode({}), 'cariai');
  assert.equal(ui.sessionMode(null), 'cariai');
  assert.deepEqual(ui.sessionsForMode(list, 'hspace').map(s => s.id), ['b', 'd']);
  assert.deepEqual(ui.sessionsForMode(list, 'cariai').map(s => s.id), ['a', 'c']);
  assert.deepEqual(ui.sessionsForMode(list, '').map(s => s.id), ['a', 'b', 'c', 'd']);
  assert.deepEqual(ui.sessionsForMode(null, 'hspace'), []);
});

test('parseHash / buildHash：Hspace 带 #/hspace 前缀，CariAi 和老地址不带；来回转一样', () => {
  assert.deepEqual(ui.parseHash('#/hspace'), { mode: 'hspace', path: 'new' });
  assert.deepEqual(ui.parseHash('#/hspace/task/s_1'), { mode: 'hspace', path: 'task/s_1' });
  assert.deepEqual(ui.parseHash('#/cariai/kb'), { mode: 'cariai', path: 'kb' });
  assert.deepEqual(ui.parseHash('#/task/s_1'), { mode: '', path: 'task/s_1' });
  assert.deepEqual(ui.parseHash(''), { mode: '', path: 'new' });
  assert.deepEqual(ui.parseHash('#/'), { mode: '', path: 'new' });
  assert.deepEqual(ui.parseHash('#/hspace/kb/lib_1/'), { mode: 'hspace', path: 'kb/lib_1' });
  assert.equal(ui.buildHash('hspace', 'new'), '#/hspace');
  assert.equal(ui.buildHash('hspace', 'kb/lib_1'), '#/hspace/kb/lib_1');
  assert.equal(ui.buildHash('cariai', 'new'), '#/new');
  assert.equal(ui.buildHash('', 'settings'), '#/settings');
  for (const h of ['#/hspace', '#/hspace/task/s_9', '#/new', '#/kb/lib_2', '#/task/x']) {
    const r = ui.parseHash(h);
    assert.equal(ui.buildHash(r.mode, r.path), h, h);
  }
});

test('withMode：没写模式的地址补当前模式；对话的地址按对话自己的模式；写了模式的不动', () => {
  assert.equal(ui.withMode('#/new', 'hspace'), '#/hspace');
  assert.equal(ui.withMode('#/kb', 'hspace'), '#/hspace/kb');
  assert.equal(ui.withMode('#/kb', 'cariai'), '#/kb');
  assert.equal(ui.withMode('#/task/s_1', 'hspace', 'cariai'), '#/task/s_1', '对话是 CariAi 的，地址不带 hspace');
  assert.equal(ui.withMode('#/task/s_1', 'cariai', 'hspace'), '#/hspace/task/s_1');
  assert.equal(ui.withMode('#/task/s_1', 'hspace', ''), '#/task/s_1', '不知道对话的模式时先不带，打开后再改');
  assert.equal(ui.withMode('#/hspace/settings', 'cariai'), '#/hspace/settings');
});

test('navItems：两个模式项目一样，Hspace 里知识库排第一、CariAi 里排后面；老后端照旧', () => {
  assert.deepEqual(ui.navItems('hspace'), ['kb', 'memory', 'schedule', 'skills', 'settings']);
  assert.deepEqual(ui.navItems('cariai'), ['memory', 'schedule', 'skills', 'kb', 'settings']);
  assert.deepEqual(ui.navItems(''), ['kb', 'memory', 'schedule', 'skills', 'settings']);
  assert.deepEqual([...ui.navItems('hspace')].sort(), [...ui.navItems('cariai')].sort());
});

console.log('出处');

const has = n => [1, 2, 3, 4, 5, 12].includes(n);

test('splitCites：[1]、[1][2]、[1, 2]、[1，2]、[1、2] 都认；不存在的编号、链接写法不认', () => {
  const cites = t => ui.splitCites(t, has).filter(p => p.cite).map(p => p.cite);
  assert.deepEqual(cites('800 元 [1]。'), [[1]]);
  assert.deepEqual(cites('起止地点[3][4]。'), [[3], [4]]);
  assert.deepEqual(cites('见 [1, 2] 和 [3，4] 和 [2、5]'), [[1, 2], [3, 4], [2, 5]]);
  assert.deepEqual(cites('[12] 有，[13] 没有'), [[12]]);
  assert.deepEqual(cites('[1, 9] 里有一个不认识就整段不认'), []);
  assert.deepEqual(cites('[1](https://a.com) 是链接'), []);
  assert.deepEqual(cites('数组 a[0] 和 [x]'), []);
  const parts = ui.splitCites('前[1]后', has);
  assert.deepEqual(parts, [{ text: '前' }, { cite: [1], raw: '[1]' }, { text: '后' }]);
  assert.deepEqual(ui.splitCites('没有出处', has), [{ text: '没有出处' }]);
});

test('citedNums：去重、按出现顺序；代码块和行内代码里的 [1] 不算；思考段不算', () => {
  const text = '结论 [2]。\n\n```python\nx = a[1]\nprint([3])\n```\n\n还有 `arr[4]` 和 [1][2]。<think>[5]</think>';
  assert.deepEqual(ui.citedNums(text, has), [2, 1]);
  assert.deepEqual(ui.citedNums('没有', has), []);
  assert.deepEqual(ui.citedNums('[7]', has), [], '不存在的编号不算');
  assert.deepEqual(ui.citedNums('~~~\n[1]\n~~~\n[3]', has), [3]);
});

test('renderMarkdown 保留 [1][2] 原样（出处标签在 DOM 上换，不在 HTML 里拼）', () => {
  const html = ui.renderMarkdown('住宿 800 元 [1][2]，见 [1, 2]。');
  assert.match(html, /\[1\]\[2\]/);
  assert.match(html, /\[1, 2\]/);
  assertSafe(html, '出处');
});

test('basisText / basisParts：依据 N 处资料；Hspace 没引用要说；核对结果追加在后面', () => {
  assert.equal(ui.basisText(3, 'hspace'), '依据 3 处资料');
  assert.equal(ui.basisText(1, ''), '依据 1 处资料');
  assert.equal(ui.basisText(0, 'hspace'), '这段回答没有引用资料');
  assert.equal(ui.basisText(0, 'cariai'), '');
  assert.equal(ui.basisText(0, ''), '');
  const g = ui.normGrounding({ msg_seq: 9, cited: [1, 2], unknown_refs: [7],
    checks: [{ n: 1, numbers: ['800', '12.4%'], missing: ['12.4%'] }, { n: 2, numbers: ['12.4%'], missing: ['12.4%'] }],
    uncited_numbers: ['3,860', '20'], ok: false });
  assert.deepEqual(ui.basisParts(3, 'hspace', g).map(p => p.text), ['依据 3 处资料', '有 1 个数字没对上', '有 2 个数字没标出处']);
  const ok = ui.normGrounding({ msg_seq: 9, cited: [1], checks: [{ n: 1, numbers: ['800'], missing: [] }], uncited_numbers: [], ok: true });
  assert.deepEqual(ui.basisParts(1, 'hspace', ok).map(p => p.text), ['依据 1 处资料'], '全对上时不多写');
  assert.deepEqual(ui.basisParts(0, '', null), []);
  assert.deepEqual(ui.basisParts(0, '', g).map(p => p.text), ['有 1 个数字没对上', '有 2 个数字没标出处'], 'CariAi 带了核对结果也写');
});

test('citeWarn / normGrounding：哪个出处里哪些数字没找到；坏输入不抛错', () => {
  const g = ui.normGrounding({ msg_seq: 3, checks: [{ n: 1, missing: ['12.4%', '20%'] }, { n: 2, missing: [] }, null, { n: 'x', missing: ['1'] }] });
  assert.equal(ui.citeWarn(g, 1), '这个数字在出处里没找到：12.4%、20%');
  assert.equal(ui.citeWarn(g, 2), '');
  assert.equal(ui.citeWarn(null, 1), '');
  assert.equal(ui.groundingMissCount(g), 2);
  assert.equal(ui.normGrounding(null), null);
  assert.equal(ui.normGrounding('x'), null);
  assert.deepEqual(ui.normGrounding({}).uncited, []);
});

const HIT = (n, extra) => Object.assign({ n, doc_id: 'd' + n, part_id: 'p' + n, library: '公司 SOP', title: '差旅管理办法.docx',
  path: '公司 SOP/差旅管理办法.docx', loc: '4.2 住宿标准', heading: '第四章', text: '每晚不超过 800 元', via: 'keyword', score: 0.9 }, extra || {});

test('reduceEvent：kb_search 的 hits 收进对话的出处表，编号不变；display.kind=kb 用资料图标', () => {
  const v = ui.newView({ id: 's', mode: 'hspace' });
  assert.equal(v.mode, 'hspace');
  const disp = { kind: 'kb', title: '在资料里找「住宿标准」', page: '', host: '', text: '' };
  ui.reduceEvent(v, { seq: 1, type: 'tool_started', payload: { call_id: 'k1', tool: 'kb_search', args: { query: '住宿' }, display: disp } });
  ui.reduceEvent(v, { seq: 2, type: 'tool_result', payload: { call_id: 'k1', tool: 'kb_search', ok: true, content: '…',
    hits: [HIT(1), HIT(2, { via: 'vector' }), { n: 0 }, null, 'x'], display: disp } });
  const it = v.items.find(x => x.kind === 'tool');
  assert.deepEqual(it.hits, [1, 2]);
  assert.deepEqual(Object.keys(v.hits), ['1', '2']);
  assert.equal(v.hits[2].via, 'vector');
  ui.reduceEvent(v, { seq: 3, type: 'tool_result', payload: { call_id: 'k2', tool: 'kb_search', ok: true, content: '…',
    hits: [HIT(2, { text: '同一段，再次被找到' }), HIT(3)] } });
  assert.deepEqual(Object.keys(v.hits), ['1', '2', '3']);
  assert.equal(v.hits[2].text, '同一段，再次被找到');
  const st = ui.stepText(it);
  assert.equal(st.verb, '在资料里找「住宿标准」');
  assert.equal(st.icon, 'book');
  assert.equal(ui.displayOf({ kind: 'nope', title: 'x' }), null);
});

test('citedSources：只列回答里确实引用了的、存在的出处，按编号排；没写完的不算', () => {
  const v = ui.newView({ id: 's' });
  ui.collectHits(v, [HIT(1), HIT(2), HIT(3)]);
  ui.reduceEvent(v, { seq: 1, type: 'assistant_message', payload: { content: '先查 [3]，再看 [1] 和 [9]。' } });
  ui.reduceEvent(v, { seq: 2, type: 'assistant_delta', payload: { text: '还在写 [2]' } });
  assert.deepEqual(ui.citedSources(v).map(h => h.n), [1, 3]);
});

test('normKbHit / hitBadges / newerText：旧版、标准答案、问答对、版本；via 的几种写法', () => {
  const old = ui.normKbHit(HIT(2, { superseded: true, newer: '《差旅管理办法（2026 版）》', version_label: '2024 版' }));
  assert.deepEqual(ui.hitBadges(old).map(b => b.text), ['旧版']);
  assert.equal(ui.newerText(old), '现行的是《差旅管理办法（2026 版）》');
  assert.equal(ui.newerText(ui.normKbHit(HIT(2, { superseded: true, newer: '差旅管理办法（2026 版）' }))), '现行的是《差旅管理办法（2026 版）》');
  assert.equal(ui.newerText(ui.normKbHit(HIT(2, { superseded: true }))), '这是旧版，资料库里有更新的版本');
  assert.equal(ui.newerText(ui.normKbHit(HIT(1))), '');
  const std = ui.normKbHit(HIT(4, { standard: true, kind: 'qa' }));
  assert.deepEqual(ui.hitBadges(std).map(b => b.text), ['标准答案']);
  assert.deepEqual(ui.hitBadges(ui.normKbHit(HIT(5, { kind: 'qa' }))).map(b => b.text), ['问答对']);
  assert.deepEqual(ui.hitBadges(ui.normKbHit(HIT(6, { version_label: '2026 版' }))).map(b => b.text), ['2026 版']);
  assert.equal(ui.normVia('both'), 'both');
  assert.equal(ui.normVia(['keyword', 'vector']), 'both');
  assert.equal(ui.normVia('hybrid'), 'both');
  assert.equal(ui.normVia('bm25'), 'keyword');
  assert.equal(ui.normVia('semantic'), 'vector');
  assert.equal(ui.normVia(''), '');
  assert.equal(ui.viaLabel('vector'), '语义');
  assert.equal(ui.viaLabel(['keyword', 'vector']), '关键词 + 语义');
  assert.equal(ui.normKbHit({ path: 'a/b/费用.pdf' }).title, '费用.pdf');
  assert.equal(ui.normKbHit(null), null);
  assert.equal(ui.hitWhere(ui.normKbHit(HIT(1))), '差旅管理办法.docx · 4.2 住宿标准');
});

test('grounding 和 feedback 事件按回答的序号挂上；不认识的、坏的不报错；刷新后还原点过的状态', () => {
  const evs = [
    { seq: 10, type: 'assistant_message', payload: { content: '800 元 [1]', tool_calls: [] } },
    { seq: 11, type: 'grounding', payload: { msg_seq: 10, cited: [1], checks: [{ n: 1, numbers: ['800'], missing: ['800'] }], uncited_numbers: [], ok: false } },
    { seq: 12, type: 'feedback', payload: { seq: 10, rating: 'up' } },
    { seq: 13, type: 'feedback', payload: { seq: 10, rating: 'down', note: '应该是 650' } },
    { seq: 14, type: 'grounding', payload: { cited: [] } },
    { seq: 15, type: 'grounding', payload: 'x' },
    { seq: 16, type: 'feedback', payload: { rating: 'up' } },
    { seq: 17, type: 'something_new', payload: { a: 1 } }
  ];
  const v = ui.buildView({ id: 's', mode: 'hspace' }, evs, [], []);
  const a = v.items.find(x => x.kind === 'assistant');
  assert.equal(a.seq, 10);
  assert.equal(a.calls, 0);
  assert.deepEqual(v.grounding[10].missing, { 1: ['800'] });
  assert.deepEqual(v.feedback[10], { rating: 'down', note: '应该是 650' });
  assert.deepEqual(Object.keys(v.grounding), ['10']);
  const v2 = ui.buildView({ id: 's' }, evs.concat([{ seq: 18, type: 'feedback', payload: { seq: 10, rating: null } }]), [], []);
  assert.equal(v2.feedback[10].rating, null, '再点一次是取消');
  assert.deepEqual(ui.normFeedback({ seq: '7', rating: 'meh' }), { seq: 7, rating: null, note: '' });
});

test('带工具调用的回答记下 calls（中间的话不写"依据"）；user_message 的 depth=deep 记成深入研究', () => {
  const v = ui.newView({ id: 's' });
  ui.reduceEvent(v, { seq: 1, type: 'user_message', payload: { text: '对比两版', depth: 'deep' } });
  ui.reduceEvent(v, { seq: 2, type: 'user_message', payload: { text: '普通的' } });
  ui.reduceEvent(v, { seq: 3, type: 'assistant_message', payload: { content: '我先查一下。', tool_calls: [{ id: 'k', name: 'kb_search', arguments: '{}' }] } });
  assert.equal(v.items[0].deep, true);
  assert.equal(v.items[1].deep, false);
  assert.equal(v.items.find(x => x.kind === 'assistant').calls, 1);
});

test('withDepth：开了深入研究才带 depth: "deep"，原请求体不动', () => {
  const b = ui.messageBody('对比', { files: ['a.xlsx'] });
  assert.deepEqual(ui.withDepth(b, true), { text: '对比', files: ['a.xlsx'], depth: 'deep' });
  assert.deepEqual(ui.withDepth(b, false), { text: '对比', files: ['a.xlsx'] });
  assert.equal(b.depth, undefined);
});

console.log('资料库');

const LIBS = ui.normalizeLibraries([
  { id: 'l1', name: '公司 SOP', sources: [{ id: 's1', path: '/x' }], docs: 128, parts: 5210, failed: 0, state: 'ready', synced: 1795240000 },
  { id: 'l2', name: '历年汇报', sources: [], docs: 37, parts: 900, state: 'indexing', progress: { total: 128, done: 37, failed: 1, current: 'a.pptx' } },
  { id: 'l3', name: '销售资料', docs: 64, failed: 3, state: 'ready' },
  { name: '没有编号的不要' }, null
]);

test('normalizeLibraries / libraryStateText：已就绪、正在收录 n/N、有几个没收进去、还没有文件', () => {
  assert.deepEqual(LIBS.map(l => l.id), ['l1', 'l2', 'l3']);
  assert.equal(LIBS[0].synced, 1795240000);
  assert.deepEqual(ui.libraryStateText(LIBS[0]), { text: '已就绪', tone: 'ok' });
  assert.deepEqual(ui.libraryStateText(LIBS[1]), { text: '正在收录 38/128', tone: 'busy' });
  assert.deepEqual(ui.libraryStateText(LIBS[2]), { text: '有 3 个文件没收进去', tone: 'warn' });
  assert.equal(ui.libraryStateText(ui.normLibrary({ id: 'x', state: 'indexing' })).text, '正在收录…');
  assert.equal(ui.libraryStateText(ui.normLibrary({ id: 'x' })).text, '还没有文件');
  assert.equal(ui.libraryStateText(ui.normLibrary({ id: 'x', sources: [{ path: '/a' }] })).text, '没有能收录的文件');
  assert.deepEqual(ui.normalizeLibraries({ libraries: [{ id: 'a' }] }).map(l => l.id), ['a']);
  assert.deepEqual(ui.normalizeLibraries('x'), []);
});

test('applyKbProgress：只改对应的库、不改原对象；做完了变成已就绪', () => {
  const p = { library_id: 'l2', total: 128, done: 100, failed: 2, current: 'b.docx' };
  const n = ui.applyKbProgress(LIBS[1], p);
  assert.notEqual(n, LIBS[1]);
  assert.equal(n.progress.done, 100);
  assert.equal(n.state, 'indexing');
  assert.equal(LIBS[1].progress.done, 37);
  assert.equal(ui.applyKbProgress(LIBS[0], p), LIBS[0]);
  assert.equal(ui.applyKbProgress(LIBS[1], { library_id: 'l2', total: 128, done: 126, failed: 2 }).state, 'ready');
});

test('scopeText / toggleScope：全部、一两个写名字、多了写几个；勾满等于全部，至少留一个', () => {
  assert.equal(ui.scopeText(LIBS, []), '全部资料库');
  assert.equal(ui.scopeText(LIBS, ['l1']), '公司 SOP');
  assert.equal(ui.scopeText(LIBS, ['l1', 'l3']), '公司 SOP、销售资料');
  assert.equal(ui.scopeText(LIBS, ['l1', 'l2', 'l3']), '全部资料库');
  assert.equal(ui.scopeText(LIBS, ['gone']), '全部资料库', '删掉的库不算');
  const four = LIBS.concat(ui.normalizeLibraries([{ id: 'l4', name: '合同' }]));
  assert.equal(ui.scopeText(four, ['l1', 'l2', 'l3']), '公司 SOP 等 3 个资料库');
  assert.deepEqual(ui.toggleScope(LIBS, [], 'l2'), ['l1', 'l3'], '从全部里去掉一个');
  assert.deepEqual(ui.toggleScope(LIBS, ['l1', 'l3'], 'l2'), [], '勾满等于全部');
  assert.deepEqual(ui.toggleScope(LIBS, ['l1'], 'l1'), ['l1'], '至少留一个');
  assert.deepEqual(ui.toggleScope(LIBS, ['l3'], 'l1'), ['l1', 'l3'], '按资料库的顺序');
});

test('kbStatusText：已收录几份、正在收录哪个；语义检索不能用时用后端给的"可以改用什么"', () => {
  const off = { ok: false, reason: '还没有接入向量模型，无法使用「语义检索」。', fallback: '现在用关键词检索：要用资料里出现过的词来问。' };
  const t = ui.kbStatusText(LIBS, off);
  assert.equal(t.head, '已收录 229 份资料');
  assert.equal(t.busy, '「历年汇报」正在收录 38/128');
  assert.equal(t.tail, off.fallback);
  assert.equal(t.reason, off.reason);
  assert.equal(ui.kbStatusText([LIBS[0]], { ok: true }).tail, '');
  assert.equal(ui.kbStatusText([LIBS[0]], { ok: false, reason: 'R' }).tail, 'R');
  assert.equal(ui.kbStatusText([], null).head, '已收录 0 份资料');
});

test('filterDocs / docStatusText / docKindText：按名字和状态筛', () => {
  const docs = [{ title: '差旅管理办法.docx', path: 'SOP/差旅管理办法.docx', status: 'ok' },
    { title: '扫描件.pdf', path: 'SOP/归档/扫描件.pdf', status: 'failed', error: '这份 PDF 是扫描件，读不出文字' },
    { title: '大文件.pdf', path: 'SOP/大文件.pdf', status: 'skipped' }, { title: '无状态.md', path: 'x.md' }, null];
  assert.equal(ui.filterDocs(docs, '', '').length, 4);
  assert.deepEqual(ui.filterDocs(docs, '', 'failed').map(d => d.title), ['扫描件.pdf']);
  assert.deepEqual(ui.filterDocs(docs, '', 'ok').map(d => d.title), ['差旅管理办法.docx', '无状态.md']);
  assert.deepEqual(ui.filterDocs(docs, '归档', '').map(d => d.title), ['扫描件.pdf'], '路径里的字也算');
  assert.deepEqual(ui.filterDocs(docs, 'PDF', 'skipped').map(d => d.title), ['大文件.pdf'], '不分大小写');
  assert.equal(ui.docStatusText('failed'), '没收进去');
  assert.equal(ui.docStatusText(''), '已收录');
  assert.equal(ui.docKindText('qa'), '问答对');
  assert.equal(ui.docKindText('slides'), 'PPT');
});

test('matchLibraries：/范围 的参数按名字、编号找；"全部"是空；有一个找不到就是 null', () => {
  assert.deepEqual(ui.matchLibraries(LIBS, '全部'), []);
  assert.deepEqual(ui.matchLibraries(LIBS, '公司 SOP'), ['l1'], '名字里有空格时先按整个名字找');
  assert.deepEqual(ui.matchLibraries(LIBS, 'SOP'), ['l1']);
  assert.deepEqual(ui.matchLibraries(LIBS, '历年汇报，销售'), ['l2', 'l3']);
  assert.deepEqual(ui.matchLibraries(LIBS, 'l3'), ['l3']);
  assert.equal(ui.matchLibraries(LIBS, '合同'), null);
  assert.equal(ui.matchLibraries(LIBS, ''), null);
});

test('normalizeCaps 带 embedding；embeddingLine 和 kbEngineText', () => {
  const c = ui.normalizeCaps({ models: [], features: [{ id: 'semantic_search', name: '语义检索', ok: false, reason: 'R', fallback: 'F' }],
    embedding: { engine: 'openai', model: 'Qwen3-Embedding-0.6B', alive: true, dims: 1024, kb_engine: 'langchain' } });
  assert.equal(c.embedding.dims, 1024);
  assert.equal(ui.embeddingLine(c.embedding), 'Qwen3-Embedding-0.6B · 在线 · 1024 维');
  assert.equal(ui.embeddingLine({ engine: 'off', model: 'x' }), '没有接向量模型');
  assert.equal(ui.kbEngineText('langchain'), 'LangChain（向量库 Chroma）');
  assert.equal(ui.kbEngineText('builtin'), '内置（SQLite 加 numpy）');
  assert.equal(ui.normalizeCaps({ models: [] }).embedding, null, '老后端没有 embedding');
  const g = ui.featureGate(c, {}, 'semantic_search');
  assert.equal(g.ok, false);
  assert.equal(g.fallback, 'F');
});

test('指令按模式：两个模式一样，Hspace 多 /范围、/深入；新后端是 /知识库，老后端是 /资料', () => {
  const names = ctx => ui.slashCommands(ctx, [], []).map(c => c.name);
  const cari = names({ mode: 'cariai' }), hs = names({ mode: 'hspace' }), old = names({});
  assert.ok(cari.includes('定时') && hs.includes('定时'), '/定时 两边都有');
  assert.ok(cari.includes('知识库') && hs.includes('知识库'));
  assert.ok(!cari.includes('资料') && !hs.includes('资料'));
  assert.ok(hs.includes('范围') && hs.includes('深入'));
  assert.ok(!cari.includes('范围') && !cari.includes('深入'));
  assert.ok(old.includes('资料') && !old.includes('知识库') && !old.includes('范围'), '老后端照旧');
  assert.deepEqual(hs.filter(n => !cari.includes(n)).sort(), ['深入', '范围'].sort());
  const deep = ui.SLASH_COMMANDS.find(c => c.id === 'deep');
  assert.equal(ui.slashAvailable(deep, { mode: 'hspace', session: true, status: 'running' }), false, '正在做事时不能 /深入');
  assert.equal(ui.slashAvailable(deep, { mode: 'hspace', session: true, status: 'done' }), true);
  assert.equal(ui.resolveSlash('/深入', ui.slashCommands({ mode: 'hspace' }, [], [])).kind, 'need-arg');
});

test('statusRows：有两种模式时第一行是"模式"（对话的模式优先）；老后端没有这一行', () => {
  assert.deepEqual(ui.statusRows({ mode: 'hspace' }, MODES_INFO)[0], { label: '模式', value: 'Hspace' });
  assert.equal(ui.statusRows({}, MODES_INFO, null, 'hspace')[0].value, 'Hspace');
  assert.equal(ui.statusRows({}, MODES_INFO)[0].value, 'CariAi');
  assert.equal(ui.statusRows({}, { model: 'm' }).find(r => r.label === '模式'), undefined);
});

test('工具动作名：kb_search / kb_open / kb_list / pptx_outline', () => {
  assert.deepEqual(ui.toolInfo('kb_search', { query: '住宿标准', library: '公司 SOP' }), { verb: '在资料里找', target: '“住宿标准”（公司 SOP）', hidden: false });
  assert.equal(ui.toolInfo('kb_open', { ref: '3' }).target, '出处 [3]');
  assert.equal(ui.toolInfo('kb_open', { ref: '差旅管理办法.docx' }).target, '差旅管理办法.docx');
  assert.equal(ui.toolInfo('kb_list', {}).verb, '看资料库里有什么');
  assert.equal(ui.toolInfo('pptx_outline', { path: 'a.pptx' }).target, 'a.pptx');
});

test('装载对话后才到的旧片段不会把回答再显示一遍（片段带 base）', () => {
  const v = ui.buildView({ id: 's1', status: 'done' }, [
    { seq: 38, session_id: 's1', type: 'user_message', payload: { text: '住宿标准？' }, ts: 1 },
    { seq: 39, session_id: 's1', type: 'tool_result', payload: { call_id: 'c1', tool: 'kb_search', ok: true, content: 'x' }, ts: 2 },
    { seq: 40, session_id: 's1', type: 'assistant_message', payload: { content: '每晚不超过 800 元。[1]', tool_calls: [] }, ts: 3 },
  ], [], []);
  const n = v.items.filter(x => x.kind === 'assistant').length;
  // 缓冲里的旧片段：那次调用从第 39 条之后开始，最终回答（第 40 条）已经在视图里
  ui.reduceEvent(v, { seq: null, session_id: 's1', type: 'assistant_delta', payload: { text: '每晚不超过 800 元。[1]', base: 39 } });
  assert.equal(v.items.filter(x => x.kind === 'assistant').length, n, '旧片段不应该新开一条回答');
  assert.equal(v.items.some(x => x.streaming), false);
  // 下一次模型调用的片段（从第 40 条之后开始）照常显示
  ui.reduceEvent(v, { seq: null, session_id: 's1', type: 'assistant_delta', payload: { text: '再补充', base: 40 } });
  assert.equal(v.items.filter(x => x.kind === 'assistant' && x.streaming).length, 1);
  // 老后端的片段没有 base：照老办法显示
  const v2 = ui.buildView({ id: 's1', status: 'running' }, [
    { seq: 40, session_id: 's1', type: 'assistant_message', payload: { content: '好。', tool_calls: [] }, ts: 3 }], [], []);
  ui.reduceEvent(v2, { seq: null, session_id: 's1', type: 'assistant_delta', payload: { text: '没有 base' } });
  assert.equal(v2.items.filter(x => x.streaming).length, 1);
});

test('内置的标准答案库不出现在资料库列表里', () => {
  const libs = ui.normalizeLibraries([
    { id: 'L_1', name: '公司 SOP', docs: 6, parts: 22, state: 'ready' },
    { id: 'lib_answers', name: '标准答案', docs: 1, parts: 1, state: 'ready', builtin: true }]);
  assert.deepEqual(libs.map(l => l.id), ['L_1']);
  assert.equal(ui.normalizeLibraries({ libraries: [{ id: 'lib_answers', name: '标准答案', builtin: true }] }).length, 0);
});


console.log('借鉴开源项目这一轮（第 7 节）');

test('normLibrary：卡片进度、语言、标签、公司共享的库；老后端的字段缺着照旧', () => {
  const l = ui.normLibrary({ id: 'L1', name: 'SOP', cards: { total: 212, ready: 180, pending: 30, failed: 2 }, lang: 'zh',
    tags: [{ tag: '报销', docs: 3 }, { tag: '财务部', docs: 9 }, 'x', { tag: '', docs: 1 }], shared: true, version: 7, enabled: false });
  assert.deepEqual([l.cards.total, l.cards.ready, l.cards.pending, l.cards.failed], [212, 180, 30, 2]);
  assert.deepEqual(l.tags.map(t => t.tag), ['财务部', '报销', 'x'], '按文档数排，空的去掉');
  assert.equal(l.shared, true); assert.equal(l.version, 7); assert.equal(l.enabled, false); assert.equal(l.v2, true);
  const old = ui.normLibrary({ id: 'L2', name: 'old' });
  assert.equal(old.cards, null); assert.deepEqual(old.tags, []); assert.equal(old.shared, false); assert.equal(old.enabled, true); assert.equal(old.v2, false);
});

test('cardStatsText / applyKbCards：摘要写了多少、后台在写；事件只改对应的库', () => {
  assert.equal(ui.cardStatsText({ total: 212, ready: 180, pending: 32, failed: 0 }), '摘要 180 / 212，后台在写');
  assert.equal(ui.cardStatsText({ total: 212, ready: 209, pending: 0, failed: 3 }), '摘要 209 / 212，3 份没写成');
  assert.equal(ui.cardStatsText({ total: 5, ready: 5, pending: 0, failed: 0 }), '摘要都写好了');
  assert.equal(ui.cardStatsText(null), '');
  assert.equal(ui.cardStatsText({ total: 0 }), '');
  const lib = ui.normLibrary({ id: 'L1', cards: { total: 10, ready: 2, pending: 8, failed: 0 } });
  const n = ui.applyKbCards(lib, { library_id: 'L1', total: 10, ready: 3, pending: 7 });
  assert.equal(n.cards.ready, 3); assert.equal(lib.cards.ready, 2, '不改原来的对象');
  assert.equal(ui.applyKbCards(lib, { library_id: 'L9', ready: 9 }), lib);
});

test('normDoc / summaryLine：卡片字段、摘要第一行；老后端没有卡片', () => {
  const d = ui.normDoc({ id: 7, title: 'a.docx', summary: '\n  第一行很重要\n第二行', summary_by: 'extract', tags: ['财务部', ' ', 3], card_state: 'ready', parts: 4 });
  assert.equal(d.id, '7'); assert.equal(d.hasCard, true); assert.deepEqual(d.tags, ['财务部', '3']); assert.equal(d.parts, 4, '原来的字段保留');
  assert.equal(ui.summaryLine(d), '第一行很重要');
  assert.equal(ui.summaryLine({ summary: 'x'.repeat(200) }, 10).length, 10);
  assert.equal(ui.normDoc({ id: 'x', title: 'b' }).hasCard, false);
  assert.equal(ui.summaryByText('extract'), '开头摘录'); assert.equal(ui.summaryByText('user'), '你改过的'); assert.equal(ui.summaryByText('model'), '');
});

test('normCard：目录、关键词、标签整理；层级超范围按 1；文字原样（不当 HTML）', () => {
  const c = ui.normCard({ doc: { id: 'd1', title: '<b>x</b>.docx', path: 'a/b.docx', kind: 'doc', library: 'SOP' }, summary: '<script>1</script>',
    summary_by: 'model', keywords: ['差旅', ''], outline: [{ level: 2, title: '4.2 住宿', loc: '4.2', section_id: 's1' }, { level: 9, title: 'x' }, null, { level: 1 }],
    tags: ['a'], lang: 'zh', card_state: 'ready', card_at: 1 });
  assert.equal(c.doc.title, '<b>x</b>.docx'); assert.equal(c.summary, '<script>1</script>');
  assert.deepEqual(c.keywords, ['差旅']);
  assert.deepEqual(c.outline.map(o => o.level + ':' + o.title), ['2:4.2 住宿', '1:x']);
  assert.equal(ui.normCard(null), null);
  assert.equal(ui.cardStateText({ card_state: 'failed' }).indexOf('没写成') >= 0, true);
  assert.equal(ui.cardStateText({ card_state: 'outline', summary: '' }), '后台还在写摘要。');
  assert.equal(ui.cardStateText({ card_state: 'ready', summary: 'x' }), '');
});

test('addTag / removeTag：1 到 20 个字、最多 10 个、不重复、去掉开头的 #', () => {
  assert.deepEqual(ui.addTag(['a'], ' 报销 ').tags, ['a', '报销']);
  assert.deepEqual(ui.addTag(['a'], '#财务').tags, ['a', '财务']);
  assert.equal(ui.addTag(['a'], 'A').error, '已经有这个标签了。');
  assert.equal(ui.addTag([], 'x'.repeat(21)).error, '标签最多 20 个字。');
  assert.equal(ui.addTag(Array.from({ length: 10 }, (_, i) => 't' + i), 'new').error, '一份文件最多 10 个标签。');
  assert.deepEqual(ui.addTag(['a'], '   ').tags, ['a']); assert.equal(ui.addTag(['a'], '').error, '');
  assert.deepEqual(ui.removeTag(['a', 'b'], 'a'), ['b']);
});

test('filterDocs：按摘要也能筛，按标签筛；老的三个参数照旧', () => {
  const docs = [{ title: 'a.docx', path: 'x/a.docx', summary: '住宿标准', tags: ['财务部'], status: 'ok' }, { title: 'b.pdf', path: 'b.pdf', tags: [], status: 'failed' }];
  assert.equal(ui.filterDocs(docs, '住宿', '').length, 1);
  assert.equal(ui.filterDocs(docs, '', '', '财务部').length, 1);
  assert.equal(ui.filterDocs(docs, '', 'failed').length, 1);
  assert.equal(ui.filterDocs(docs, '', '', '没有').length, 0);
});

const SLIBS = [{ id: 'l1', name: '公司 SOP' }, { id: 'l2', name: '历年汇报' }, { id: 'l3', name: '销售资料' }];
test('检索范围：scopeBody 只带有值的；scopeChips 资料库没选全部才列；scopeLabel 几样连起来', () => {
  assert.deepEqual(ui.scopeBody({ libraries: [], docs: ['d1'], tags: [] }), { docs: ['d1'] });
  assert.deepEqual(ui.scopeBody({}), {});
  assert.equal(ui.scopeEmpty({ tags: [] }), true); assert.equal(ui.scopeEmpty({ tags: ['x'] }), false);
  const chips = ui.scopeChips(SLIBS, { libraries: ['l1'], tags: ['报销'], docs: ['d1', 'd2'] }, { d1: '差旅管理办法.docx' });
  assert.deepEqual(chips.map(c => c.kind + ':' + c.label), ['lib:公司 SOP', 'tag:标签：报销', 'doc:差旅管理办法.docx', 'doc:一份文件']);
  assert.equal(ui.scopeChips(SLIBS, { libraries: ['l1', 'l2', 'l3'] }).length, 0, '全选等于全部');
  assert.equal(ui.scopeChips(SLIBS, { libraries: ['gone'] }).length, 0, '删掉的库不算');
  assert.equal(ui.scopeLabel(SLIBS, {}), '全部资料库');
  assert.equal(ui.scopeLabel(SLIBS, { tags: ['报销'] }), '标签：报销');
  assert.equal(ui.scopeLabel(SLIBS, { libraries: ['l1'], docs: ['d1'] }, { d1: 'x/差旅.docx' }), '公司 SOP · 差旅.docx');
  assert.equal(ui.scopeLabel(SLIBS, { docs: ['a', 'b', 'c'] }), '3 份文件');
  assert.equal(ui.scopeLabel(SLIBS, { tags: ['a', 'b', 'c'] }), '标签：a 等 3 个');
  assert.deepEqual(ui.toggleIn(['a'], 'b'), ['a', 'b']); assert.deepEqual(ui.toggleIn(['a', 'b'], 'a'), ['b']);
  assert.deepEqual(ui.mergeTags([{ tags: [{ tag: 'x', docs: 2 }] }, { tags: [{ tag: 'x', docs: 3 }, { tag: 'y', docs: 9 }] }]), [{ tag: 'y', docs: 9 }, { tag: 'x', docs: 5 }]);
});

test('hashQuery：# 或全角 ＃，前面是开头或空白，到光标之间没有空白', () => {
  assert.deepEqual(ui.hashQuery('#差旅', 3), { start: 0, end: 3, q: '差旅' });
  assert.deepEqual(ui.hashQuery('看看 ＃预算', 6), { start: 3, end: 6, q: '预算' });
  assert.deepEqual(ui.hashQuery('看看 #', 4), { start: 3, end: 4, q: '' });
  assert.equal(ui.hashQuery('C#语言', 4), null, '# 前面紧挨着字不算');
  assert.equal(ui.hashQuery('#差旅 管理', 6), null, '光标前有空白就不算');
  assert.equal(ui.hashQuery('没有井号', 4), null);
  assert.deepEqual(ui.hashQuery('#ab 后面', 2), { start: 0, end: 2, q: 'a' }, '按光标位置算');
  assert.equal(ui.hashQuery('#' + 'x'.repeat(41)), null, '太长了不当文件名');
});

test('dropHashToken：去掉 #… 并把光标放回去；两边的空白只留一个', () => {
  assert.deepEqual(ui.dropHashToken('这份 #差旅 讲了什么', { start: 3, end: 6 }), { value: '这份 讲了什么', caret: 3 });
  assert.deepEqual(ui.dropHashToken('#差旅', { start: 0, end: 3 }), { value: '', caret: 0 });
  assert.deepEqual(ui.dropHashToken('abc', null), { value: 'abc', caret: 3 });
});

test('searchDocs：文件名命中排在摘要命中前面；没收进去的不列；空查询列前几份', () => {
  const docs = [
    { id: '1', title: '费用报销 SOP.pdf', summary: '报销流程', status: 'ok' },
    { id: '2', title: '差旅管理办法.docx', summary: '出差住宿和报销标准', status: 'ok' },
    { id: '3', title: '报销单模板.xlsx', summary: '', status: 'failed' },
    { id: '4', title: '组织架构.pdf', summary: '', tags: ['报销'], status: 'ok' }];
  assert.deepEqual(ui.searchDocs(docs, '报销').map(d => d.id), ['1', '2', '4']);
  assert.deepEqual(ui.searchDocs(docs, '住宿').map(d => d.id), ['2']);
  assert.equal(ui.searchDocs(docs, '').length, 3);
  assert.equal(ui.searchDocs(docs, '', 2).length, 2);
  assert.deepEqual(ui.searchDocs(docs, '不存在'), []);
});

test('parseTplParams：四种写法、同名只算一次、写法不对的跳过', () => {
  const ps = ui.parseTplParams('分析 {{月份:month}} 的 {{部门:choice=销售|市场|财务}}，{{备注?}}；再看 {{月份}} 和 {{ 负责人 }}');
  assert.deepEqual(ps.map(p => [p.name, p.type, p.required]), [['月份', 'month', true], ['部门', 'choice', true], ['备注', 'text', false], ['负责人', 'text', true]]);
  assert.deepEqual(ps[1].options, ['销售', '市场', '财务']);
  assert.deepEqual(ui.parseTplParams('{{x:dept}} {{y:choice}} {{:text}} {{}} {{z？}}').map(p => p.name + p.required), ['zfalse'], '类型不认识、choice 没选项、没名字的都跳过；全角问号也算可选');
  assert.equal(ui.parseTplParams('{{日期:DATE}}')[0].type, 'date', '类型不分大小写');
  assert.equal(ui.parseTplParams('{{文件:file?}}')[0].required, false, '问号写在类型后面也算');
  assert.equal(ui.parseTplParams('没有空').length, 0);
  assert.deepEqual(ui.tplSlotProblems('{{a:dept}} {{b}} {{a:dept}}'), ['{{a:dept}}']);
  assert.deepEqual(ui.tplParamsText(ui.parseTplParams('{{部门:choice=甲|乙}}{{备注?}}')), ['部门（选一个：甲 / 乙）', '备注（文字，可不填）']);
});

test('tplParams：后端给了 params 以它为准；没有就从 prompt 里解析', () => {
  const t = { prompt: '{{a}}', params: [{ name: '月份', type: 'month' }, { name: '部门', type: 'choice' }, { name: '月份' }, { label: '没名字' }, { name: 'x', required: false, default: 3 }] };
  assert.deepEqual(ui.tplParams(t).map(p => [p.name, p.type, p.required, p.default]), [['月份', 'month', true, ''], ['部门', 'text', true, ''], ['x', 'text', false, '3']], 'choice 没选项退成文字');
  assert.deepEqual(ui.tplParams({ prompt: '{{a}}', params: [] }).map(p => p.name), ['a']);
  assert.deepEqual(ui.tplParams(null), []);
});

test('renderTpl：填进去；可选的没填连同前面的标点一起去掉；填的值里的 {{}} 不再解析', () => {
  const p = '分析 {{月份:month}} 的经营数据，重点看{{部门:choice=销售|市场}}，{{备注?}}';
  assert.equal(ui.renderTpl(p, { 月份: '2026-10', 部门: '市场' }), '分析 2026-10 的经营数据，重点看市场');
  assert.equal(ui.renderTpl(p, { 月份: '2026-10', 部门: '市场', 备注: '对比去年' }), '分析 2026-10 的经营数据，重点看市场，对比去年');
  assert.equal(ui.renderTpl('看 {{a}}', { a: '{{b}}' }), '看 {{b}}');
  assert.equal(ui.renderTpl('看 {{a}} 和 {{b}}', { a: '1' }), '看 1 和 {{b}}', '必填没填的原样留着');
  assert.equal(ui.renderTpl('写法不对 {{x:dept}}', {}), '写法不对 {{x:dept}}');
  assert.deepEqual(ui.tplMissing(ui.parseTplParams(p), { 月份: ' ' }), ['月份', '部门']);
  assert.deepEqual(ui.tplMissing(ui.parseTplParams(p), { 月份: '1', 部门: '2' }), []);
});

test('groupTemplates：带着 id、params、source', () => {
  const g = ui.groupTemplates([{ id: 'm', title: '月度', prompt: '{{月份}}', role: '经营', source: 'builtin', params: [{ name: '月份' }] }, { title: 'x', prompt: 'y', source: 'user' }]);
  assert.equal(g[0].items[0].id, 'm'); assert.equal(g[0].items[0].source, 'builtin'); assert.equal(g[0].items[0].params.length, 1);
  assert.equal(g[1].items[0].id, ''); assert.equal(g[1].items[0].params, null);
  assert.equal(ui.tplSourceText('shared'), '公司'); assert.equal(ui.tplSourceText('user'), '我的'); assert.equal(ui.tplSourceText('builtin'), '');
});

test('slashCommands：带参数的模板带着模板本身，参数提示写"填几个空"', () => {
  const groups = ui.groupTemplates([{ id: 'm', title: '月度分析', prompt: '分析 {{月份:month}}' }, { title: '写周报', prompt: '写周报' }]);
  const list = ui.slashCommands({ mode: '' }, [], groups);
  const m = list.find(c => c.name === '月度分析'), w = list.find(c => c.name === '写周报');
  assert.equal(m.tpl.id, 'm'); assert.equal(m.arg, '（填几个空）');
  assert.equal(w.arg, '');
  assert.equal(ui.slashFill(m), '分析 {{月份:month}}', 'slashFill 照旧（界面在确认时改走表单）');
});

test('没答上来的问题：整理、排序（次数再最近一次）、原因的说法', () => {
  const g = ui.normGaps([{ id: 1, question: 'a', reason: 'no_hit', count: 1, last_ts: 50 }, { id: 2, question: 'b', reason: 'down', count: 3, last_ts: 10 },
    { id: 3, question: 'c', count: 1, last_ts: 90 }, { id: 4, question: '  ' }, null]);
  assert.deepEqual(g.map(x => x.id), ['2', '3', '1']);
  assert.equal(ui.gapReasonText('no_hit'), '资料里没找到'); assert.equal(ui.gapReasonText('down'), '回答被标了不对');
  assert.equal(ui.normGaps({ gaps: [{ id: 'x', question: 'q' }] })[0].count, 1, '次数至少 1');
  assert.equal(ui.normGap({ id: 'x', question: '<b>q</b>' }).question, '<b>q</b>');
});

test('标准答案：用过 N 次；出处标签上写"标准答案 · 用过 N 次"', () => {
  assert.equal(ui.answerUseText({ hits: 14 }), '用过 14 次'); assert.equal(ui.answerUseText({ hits: 0 }), '还没用过'); assert.equal(ui.answerUseText({}), '');
  assert.deepEqual(ui.hitBadges(ui.normKbHit({ n: 1, standard: true, hits: 1234 })).map(b => b.text), ['标准答案 · 用过 1,234 次']);
  assert.deepEqual(ui.hitBadges(ui.normKbHit({ n: 1, standard: true })).map(b => b.text), ['标准答案'], '老后端照旧');
  assert.equal(ui.normKbHit({ n: 1, exact: true }).exact, true);
});

test('检索评测：应命中的文件分开、命中率、和上次比', () => {
  assert.deepEqual(ui.parseExpect('差旅管理办法；费用报销 SOP;差旅管理办法\n预算表、'), ['差旅管理办法', '费用报销 SOP', '预算表']);
  const e = ui.normEval({ items: [{ id: 1, question: 'q', expect: ['a'] }, { id: 2, question: '' }], runs: [{ id: 'r1', ts: 1, total: 4, hit1: 2, hit3: 3, hit5: 3 }, { id: 'r2', ts: 2, total: 4, hit1: 3, hit3: 3, hit5: 4 }] });
  assert.equal(e.items.length, 1); assert.deepEqual(e.runs.map(r => r.id), ['r2', 'r1'], '新的在前');
  assert.equal(ui.evalPct(e.runs[0], 5), 100); assert.equal(ui.evalPct({ total: 0 }, 1), 0);
  const cmp = ui.evalCompare(e.runs[0], e.runs[1]);
  assert.deepEqual(cmp.map(c => [c.k, c.pct, c.delta]), [[1, 75, 25], [3, 75, 0], [5, 100, 25]]);
  assert.equal(ui.evalCompare(e.runs[0], null)[0].delta, null);
  assert.equal(ui.deltaText(5), '比上次高 5 点'); assert.equal(ui.deltaText(-3), '比上次低 3 点'); assert.equal(ui.deltaText(0), '和上次一样'); assert.equal(ui.deltaText(null), '');
  const run = ui.normEvalRun({ id: 'x', total: 2, misses: [{ question: 'q', expect: ['a'], got: ['b', 'c'] }] });
  assert.equal(run.hasMisses, true); assert.deepEqual(run.misses[0].got, ['b', 'c']);
  assert.equal(ui.normEvalRun({ id: 'y', total: 2 }).hasMisses, false, '历次结果不带明细，不能说"都找到了"');
});

test('工具组：tool_load 的步骤写"加载工具：操作网页"；/状态 写带了几组', () => {
  assert.equal(ui.toolInfo('tool_load', { group: 'browser' }).verb, '加载工具：操作网页');
  assert.equal(ui.toolInfo('tool_load', '{"group":"mcp:oa"}').verb, '加载工具：外部系统：oa');
  assert.equal(ui.toolInfo('tool_load', {}).verb, '加载工具');
  assert.equal(ui.toolsetName('excel', [{ id: 'excel', name: '读、查、比、生成 Excel' }]), '读、查、比、生成 Excel', '后端给的名字优先');
  const ts = { lazy: true, loaded: ['core', 'kb', 'excel', 'docs', 'browser'], unloaded: ['desktop', 'web', 'code', 'auto'], chars: 6120, chars_all: 12941 };
  assert.equal(ui.toolsetLine(ts), '带了 5 组（约 6,100 字符），没带 4 组');
  assert.equal(ui.toolsetLine({ lazy: false, loaded: [], unloaded: [], chars: 12941 }), '全部带上（约 12,900 字符）');
  assert.equal(ui.toolsetLine(null), '');
  const rows = ui.statusRows({ toolset: ts }, {});
  const r = rows.find(x => x.label === '工具');
  assert.equal(r.value, '带了 5 组（约 6,100 字符），没带 4 组'); assert.deepEqual(r.toolset.unloaded, ts.unloaded);
  assert.equal(ui.statusRows({}, {}).some(x => x.label === '工具'), false, '老后端没有这一行');
});

test('loop_guard 事件：那一步标成被拦下；折叠行不算出错，单独说', () => {
  seq = 0;
  const v = ui.newView({ id: 's1', status: 'idle' });
  ui.reduceEvent(v, ev('user_message', { text: '查' }));
  ui.reduceEvent(v, ev('assistant_message', { content: '', tool_calls: [{ id: 'c1', name: 'kb_search', arguments: { query: 'x' } }] }));
  ui.reduceEvent(v, ev('tool_started', { call_id: 'c1', tool: 'kb_search', args: { query: 'x' } }));
  ui.reduceEvent(v, ev('loop_guard', { tool: 'kb_search', count: 3, call_id: 'c1' }));
  ui.reduceEvent(v, ev('tool_result', { call_id: 'c1', tool: 'kb_search', ok: false, content: '这个调用和前两次完全一样' }));
  const it = v.items.find(x => x.kind === 'tool');
  assert.equal(it.looped, 3); assert.equal(it.state, 'failed');
  const f = ui.foldSteps(v.items.filter(x => x.kind === 'tool'), {});
  assert.equal(f.failed, 0); assert.equal(f.looped, 1);
  assert.equal(f.text, '做了 1 步，拦下 1 次重复的调用');
  // 没有对应的步骤时也不出错（新建一条）
  ui.reduceEvent(v, ev('loop_guard', { tool: 'browser_look', count: 2 }));
  assert.equal(v.items.filter(x => x.kind === 'tool' && x.looped).length, 2);
});

test('对话的范围：meta 里的 docs、tags 读进视图', () => {
  const v = ui.newView({ id: 's', meta: { libraries: ['l1'], docs: ['d1', 2], tags: ['报销'] } });
  assert.deepEqual(v.docs, ['d1', '2']); assert.deepEqual(v.tags, ['报销']);
  const o = ui.newView({ id: 's', meta: {} });
  assert.deepEqual(o.docs, []); assert.deepEqual(o.tags, []);
});

test('被公司锁住的设置：本身、上一级都算；一节里列出锁住的；中文名', () => {
  const locked = ['endpoints', 'kb', 'browser.allowed_domains'];
  assert.equal(ui.isLocked(locked, 'endpoints'), true);
  assert.equal(ui.isLocked(locked, 'kb.top_k'), true, '上一级锁住了');
  assert.equal(ui.isLocked(locked, 'browser'), false, '只锁了下面一项，整节不算');
  assert.equal(ui.isLocked(locked, 'endpointsx'), false);
  assert.deepEqual(ui.lockedUnder(locked, 'browser'), ['browser.allowed_domains']);
  assert.deepEqual(ui.lockedUnder(locked, 'kb.cards'), ['kb']);
  assert.deepEqual(ui.lockedUnder([], 'asr'), []);
  assert.equal(ui.settingKeyText('tools.lazy'), '工具按需加载');
  assert.equal(ui.settingKeyText('kb.foo'), '资料库检索（kb.foo）');
  assert.equal(ui.settingKeyText('zzz'), 'zzz');
});

test('公司共享目录：整理、状态一句话、库的版本那一行', () => {
  const s = ui.normShared({ dir: '\\\\fs\\CA', configured: true, ok: false, error: '找不到网络路径', synced: 1000, name: '某某公司',
    settings: { locked: ['endpoints'], defaults: ['ui.theme'], ignored: [{ key: 'mcp_servers', why: '不在可以下发的范围里' }, {}] },
    libraries: [{ id: 'l', name: '公司制度', version: 7, local_version: 6, state: 'outdated', vectors: 'mismatch' }, { name: '没编号' }], notes: ['有写权限'] });
  assert.deepEqual(s.locked, ['endpoints']); assert.equal(s.ignored.length, 1); assert.equal(s.libraries.length, 1);
  assert.equal(ui.sharedStateText(s, 1000 * 1000 + 3600 * 1000).text, '连不上，现在用的是上次同步的内容（' + ui.formatTime(1000, 1000 * 1000 + 3600 * 1000) + '）。找不到网络路径');
  assert.equal(ui.sharedStateText(ui.normShared({ dir: 'x', checking: true })).text, '正在检查…');
  const sl = ui.normShared({ dir: 'x', libraries: [{ id: 'l', name: 'a', vectors_why: '本机没有配向量模型', error: 'e', enabled: false }], checking: false, sync_minutes: 0 });
  assert.equal(sl.libraries[0].vectors_why, '本机没有配向量模型'); assert.equal(sl.libraries[0].enabled, false); assert.equal(sl.sync_minutes, 0);
  assert.equal(ui.sharedStateText(ui.normShared({ dir: 'x', ok: true, name: 'A' })).text, '连上了：A');
  assert.equal(ui.sharedStateText(ui.normShared({})).tone, 'empty');
  assert.equal(ui.sharedLibText(s.libraries[0]), '本机第 6 版，有第 7 版');
  assert.equal(ui.sharedLibText({ state: 'updating', version: 8 }), '正在更新到第 8 版');
  assert.equal(ui.sharedLibText({ state: 'ok', version: 7 }), '第 7 版');
});

test('技能：公司共享目录的技能 source 是 shared，排在我的之后、内置之前；指令说明前写"公司"', () => {
  const r = ui.normalizeSkills([{ name: 'a', source: 'builtin' }, { name: 'b', source: 'shared', description: '周报' }, { name: 'c', source: 'user' }]);
  assert.deepEqual(r.map(s => s.name + ':' + s.source), ['c:user', 'b:shared', 'a:builtin']);
  const cmd = ui.slashCommands({ mode: '' }, r, []).find(c => c.name === 'b');
  assert.equal(cmd.desc, '公司 · 周报');
});

test('langText：中文为主、英文为主、中英混合', () => {
  assert.equal(ui.langText('zh'), '中文为主'); assert.equal(ui.langText('en'), '英文为主'); assert.equal(ui.langText('mixed'), '中英混合'); assert.equal(ui.langText(''), '');
});


console.log('对着真后端联调');

test('cardStatsText：mode 不是 model 时不说"后台在写"，原因另给一行', () => {
  const ex = { total: 6, ready: 0, pending: 6, failed: 0, extract: 6, mode: 'extract', reason: '演示模式没有真模型，摘要先用开头摘录。', writing: false };
  assert.equal(ui.cardStatsText(ex), '摘要先用开头摘录');
  assert.equal(ui.cardsReason(ex), '演示模式没有真模型，摘要先用开头摘录。');
  assert.equal(ui.cardsBusy(ex), false);
  assert.equal(ui.cardStatsText({ total: 6, ready: 2, pending: 4, extract: 4, mode: 'extract' }), '摘要 2 / 6，其余 4 份先用开头摘录');
  assert.equal(ui.cardStatsText({ total: 6, ready: 0, mode: 'off', reason: 'x' }), '文档摘要关着，只有目录和开头摘录');
  const m = { total: 6, ready: 2, pending: 4, mode: 'model', writing: true, reason: '' };
  assert.equal(ui.cardStatsText(m), '摘要 2 / 6，后台在写'); assert.equal(ui.cardsBusy(m), true); assert.equal(ui.cardsReason(m), '');
  assert.equal(ui.cardStatsText({ total: 6, ready: 2, pending: 4, mode: 'model', writing: false }), '摘要 2 / 6，4 份等着写');
});

test('工具组：状态里的 groups 名字优先；后备表有 analysis、kb_add、memory', () => {
  const t = ui.normToolset({ lazy: true, loaded: ['core'], unloaded: ['analysis'], chars: 5954, chars_all: 11845,
    groups: [{ id: 'core', name: '常用工具', loaded: true }, { id: 'analysis', name: '查询和比对表格' }, null] });
  assert.equal(t.groups.length, 2);
  assert.equal(ui.toolsetName('core', t.groups), '常用工具');
  assert.equal(ui.toolsetName('kb_add'), '收录资料'); assert.equal(ui.toolsetName('memory'), '记忆和历史'); assert.equal(ui.toolsetName('analysis'), '查询和比对表格');
});

test('tool_load 的 display（kind: tool_load）：步骤行直接用它的标题', () => {
  const st = ui.stepText({ tool: 'tool_load', args: { group: 'browser' }, display: { kind: 'tool_load', title: '加载工具：操作网页', group: 'browser' } });
  assert.equal(st.verb, '加载工具：操作网页'); assert.equal(st.icon, 'tools');
});

test('loop_guard 先到、紧接着 tool_result（ok:false），没有 tool_started：一步，标成被拦下', () => {
  seq = 0;
  const v = ui.newView({ id: 's1', status: 'idle' });
  ui.reduceEvent(v, ev('assistant_message', { content: '', tool_calls: [{ id: 'm3_0', name: 'list_dir', arguments: { path: '.' } }] }));
  ui.reduceEvent(v, ev('loop_guard', { tool: 'list_dir', count: 3, call_id: 'm3_0' }));
  ui.reduceEvent(v, ev('tool_result', { call_id: 'm3_0', tool: 'list_dir', ok: false, content: '这个调用和前两次完全一样' }));
  const steps = v.items.filter(x => x.kind === 'tool');
  assert.equal(steps.length, 1); assert.equal(steps[0].looped, 3);
});

test('评测：返回的 mode 是实际用的方式', () => {
  assert.equal(ui.evalModeText('keyword'), '只用了关键词'); assert.equal(ui.evalModeText('hybrid'), '关键词 + 语义');
});

console.log(`\n${passed} 通过，${failed} 失败`);
if (failed) process.exit(1);
