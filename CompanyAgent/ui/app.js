/* 公司本机办公 Agent 界面。
 * 不用任何框架。文件分两部分：
 *   一、纯函数（Markdown 渲染、转义、事件归并、工具动作名等），不碰 DOM，node 里可测试；
 *   二、界面（只在浏览器里运行）。
 * 安全约定：模型输出、文件内容、工具结果一律不可信。界面只在一个地方用 innerHTML：
 * 插入 renderMarkdown() 的结果，而它先转义全部 HTML，再按固定白名单加标签。
 */
(function () {
  'use strict';

  /* ================================================================
   * 一、纯函数
   * ================================================================ */

  var ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return ESC[c]; });
  }

  function unescapeHtml(s) {
    return String(s).replace(/&(amp|lt|gt|quot|#39);/g, function (m, k) {
      return { amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'" }[k];
    });
  }

  /** 只放行 http/https 链接；其余（javascript:、data:、相对路径等）返回 null。 */
  function safeUrl(raw) {
    if (typeof raw !== 'string') return null;
    var u = raw.trim();
    if (!u || u.length > 2000) return null;
    if (/[\u0000- \u007f-\u009f"'<>`\\]/.test(u)) return null;
    if (!/^https?:\/\/[^\/?#]+/i.test(u)) return null;
    return u;
  }

  /** 去掉模型的思考段落（<think>…</think>），未闭合时把后面全部隐去。 */
  function stripThink(text) {
    var s = String(text == null ? '' : text);
    s = s.replace(/<think>[\s\S]*?<\/think>\s*/gi, '');
    var i = s.search(/<think>/i);
    if (i >= 0) s = s.slice(0, i);
    return s;
  }

  var PH = '\u0000';

  /** 行内 Markdown：行内代码、链接、自动链接、加粗、斜体、删除线。输入是原始文字，输出是安全 HTML。 */
  function renderInline(text) {
    var slots = [];
    function keep(html) { slots.push(html); return PH + (slots.length - 1) + PH; }
    var s = String(text == null ? '' : text).replace(/\u0000/g, '');

    // 1. 行内代码：原样显示
    s = s.replace(/`([^`\n]+)`/g, function (m, code) {
      return keep('<code>' + escapeHtml(code) + '</code>');
    });
    // 2. [文字](链接)
    s = s.replace(/\[([^\]\n]{1,500})\]\(([^)\s]{1,2000})(?:\s+"[^"\n]*")?\)/g, function (m, label, url) {
      var inner = emphasis(escapeHtml(label));
      var ok = safeUrl(url);
      if (!ok) return keep('<span class="md-badlink">' + inner + '</span>');
      return keep('<a href="' + escapeHtml(ok) + '" target="_blank" rel="noopener noreferrer">' + inner + '</a>');
    });
    // 3. 裸露的网址
    s = s.replace(/https?:\/\/[^\s\u0000<>"'`）)，。、；！？\]]+/gi, function (m) {
      var tail = '';
      while (/[.,;:!?]$/.test(m)) { tail = m.slice(-1) + tail; m = m.slice(0, -1); }
      var ok = safeUrl(m);
      if (!ok) return m + tail;
      return keep('<a href="' + escapeHtml(ok) + '" target="_blank" rel="noopener noreferrer">' + escapeHtml(m) + '</a>') + tail;
    });
    // 4. 其余文字一律转义，再加强调标签
    s = emphasis(escapeHtml(s));
    // 5. 放回占位内容（链接文字里可能套着行内代码，最多展开三层）
    for (var k = 0; k < 3 && s.indexOf(PH) >= 0; k++) {
      s = s.replace(/\u0000(\d+)\u0000/g, function (m, i) { return slots[+i] == null ? '' : slots[+i]; });
    }
    return s.replace(/\u0000/g, '');
  }

  /** 在已经转义过的文字上加强调标签。只插入固定标签，不引入任何属性。 */
  function emphasis(escaped) {
    return escaped
      .replace(/\*\*([^*\n]+?)\*\*/g, '<strong>$1</strong>')
      .replace(/__([^_\n]+?)__/g, '<strong>$1</strong>')
      .replace(/~~([^~\n]+?)~~/g, '<del>$1</del>')
      .replace(/(^|[^*\w])\*([^*\s][^*\n]*?)\*(?!\*)/g, '$1<em>$2</em>');
  }

  var RE_FENCE = /^\s{0,3}(`{3,}|~{3,})\s*([^\s`]*)/;
  var RE_HEADING = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
  var RE_HR = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
  var RE_LIST = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
  var RE_QUOTE = /^\s{0,3}>\s?(.*)$/;
  var RE_TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

  function splitRow(line) {
    var s = line.trim();
    if (s.charAt(0) === '|') s = s.slice(1);
    if (s.charAt(s.length - 1) === '|' && s.charAt(s.length - 2) !== '\\') s = s.slice(0, -1);
    var cells = [], cur = '';
    for (var i = 0; i < s.length; i++) {
      var c = s.charAt(i);
      if (c === '\\' && s.charAt(i + 1) === '|') { cur += '|'; i++; continue; }
      if (c === '|') { cells.push(cur.trim()); cur = ''; continue; }
      cur += c;
    }
    cells.push(cur.trim());
    return cells;
  }

  function isTableStart(lines, i) {
    return lines[i].indexOf('|') >= 0 && i + 1 < lines.length && RE_TABLE_SEP.test(lines[i + 1]) &&
      lines[i + 1].indexOf('-') >= 0;
  }

  function isBlockStart(lines, i) {
    var l = lines[i];
    return RE_FENCE.test(l) || RE_HEADING.test(l) || RE_HR.test(l) || RE_LIST.test(l) || RE_QUOTE.test(l) ||
      isTableStart(lines, i);
  }

  function indentOf(s) {
    var n = 0;
    for (var i = 0; i < s.length; i++) {
      if (s.charAt(i) === ' ') n += 1; else if (s.charAt(i) === '\t') n += 4; else break;
    }
    return n;
  }

  function buildList(tokens, pos) {
    var base = tokens[pos].indent;
    var ordered = tokens[pos].ordered;
    var open = ordered ? '<ol' + (tokens[pos].start > 1 ? ' start="' + tokens[pos].start + '"' : '') + '>' : '<ul>';
    var html = open;
    while (pos < tokens.length && tokens[pos].indent >= base) {
      var t = tokens[pos];
      if (t.indent === base && t.ordered !== ordered) break;   // 有序和无序换了，另起一个列表
      if (t.indent > base && /<\/li>$/.test(html)) {
        var r = buildList(tokens, pos);
        html = html.replace(/<\/li>$/, r.html + '</li>');
        pos = r.pos;
        continue;
      }
      var body = t.text, check = '';
      var m = /^\[([ xX])\]\s+(.*)$/.exec(body);
      if (m) { check = '<span class="md-check">' + (m[1] === ' ' ? '[ ]' : '[x]') + '</span> '; body = m[2]; }
      html += '<li>' + check + body.split('\n').map(renderInline).join('<br>') + '</li>';
      pos++;
    }
    return { html: html + (ordered ? '</ol>' : '</ul>'), pos: pos };
  }

  /** 块级 Markdown。depth 用于限制引用的嵌套层数。 */
  function renderMarkdown(src, depth) {
    depth = depth || 0;
    var text = stripThink(String(src == null ? '' : src)).replace(/\r\n?/g, '\n').replace(/\u0000/g, '');
    var lines = text.split('\n');
    var out = [];
    var i = 0;
    while (i < lines.length) {
      var line = lines[i];
      var m;
      if (!line.trim()) { i++; continue; }

      if ((m = RE_FENCE.exec(line))) {
        var fence = m[1], body = [];
        i++;
        var closeRe = new RegExp('^\\s{0,3}' + (fence.charAt(0) === '`' ? '`' : '~') + '{' + fence.length + ',}\\s*$');
        while (i < lines.length && !closeRe.test(lines[i])) { body.push(lines[i]); i++; }
        i++; // 跳过结尾的围栏（没有也无妨）
        out.push('<pre class="md-code"><code>' + escapeHtml(body.join('\n')) + '</code></pre>');
        continue;
      }
      if ((m = RE_HEADING.exec(line))) {
        var lv = m[1].length;
        out.push('<h' + lv + '>' + renderInline(m[2]) + '</h' + lv + '>');
        i++; continue;
      }
      if (RE_HR.test(line)) { out.push('<hr>'); i++; continue; }

      if (isTableStart(lines, i)) {
        var head = splitRow(lines[i]);
        var aligns = splitRow(lines[i + 1]).map(function (c) {
          var l = c.charAt(0) === ':', r = c.charAt(c.length - 1) === ':';
          return l && r ? 'center' : r ? 'right' : '';
        });
        var html = '<div class="md-table"><table><thead><tr>';
        head.forEach(function (c, k) {
          html += '<th' + (aligns[k] ? ' class="al-' + aligns[k] + '"' : '') + '>' + renderInline(c) + '</th>';
        });
        html += '</tr></thead><tbody>';
        i += 2;
        while (i < lines.length && lines[i].trim() && lines[i].indexOf('|') >= 0) {
          var cells = splitRow(lines[i]);
          html += '<tr>';
          for (var k = 0; k < head.length; k++) {
            html += '<td' + (aligns[k] ? ' class="al-' + aligns[k] + '"' : '') + '>' + renderInline(cells[k] || '') + '</td>';
          }
          html += '</tr>';
          i++;
        }
        out.push(html + '</tbody></table></div>');
        continue;
      }

      if (RE_QUOTE.test(line)) {
        var q = [];
        while (i < lines.length && (m = RE_QUOTE.exec(lines[i]))) { q.push(m[1]); i++; }
        out.push('<blockquote>' + (depth < 3 ? renderMarkdown(q.join('\n'), depth + 1)
          : '<p>' + q.map(renderInline).join('<br>') + '</p>') + '</blockquote>');
        continue;
      }

      if (RE_LIST.test(line)) {
        var tokens = [];
        while (i < lines.length) {
          var l = lines[i];
          var lm = RE_LIST.exec(l);
          if (lm) {
            tokens.push({ indent: indentOf(lm[1]), ordered: /\d/.test(lm[2]), start: parseInt(lm[2], 10) || 1, text: lm[3] });
            i++; continue;
          }
          if (l.trim() && tokens.length && indentOf(l) >= 2 && !RE_FENCE.test(l)) {
            tokens[tokens.length - 1].text += '\n' + l.trim();
            i++; continue;
          }
          // 列表项之间的单个空行：后面还是列表就继续
          if (!l.trim() && i + 1 < lines.length && RE_LIST.test(lines[i + 1])) { i++; continue; }
          break;
        }
        // 同一层第一个缩进作为基准，避免第一项就缩进时出错
        var minIndent = Math.min.apply(null, tokens.map(function (t) { return t.indent; }));
        tokens.forEach(function (t) { t.indent -= minIndent; });
        var pos = 0;
        while (pos < tokens.length) {
          var r = buildList(tokens, pos);
          out.push(r.html);
          pos = r.pos;
        }
        continue;
      }

      var para = [];
      while (i < lines.length && lines[i].trim() && (para.length === 0 || !isBlockStart(lines, i))) {
        para.push(lines[i]); i++;
      }
      out.push('<p>' + para.map(function (p) { return renderInline(p.trim()); }).join('<br>') + '</p>');
    }
    return out.join('\n');
  }

  /* ---------------- 工具动作名 ---------------- */

  var RISK_LABEL = {
    none: '无风险', read: '只读', create: '新建', overwrite: '覆盖', delete: '删除',
    exec: '执行代码', external: '对外', memory: '记忆', skill: '技能', web: '网页操作', submit: '提交',
    read_outside: '读文件夹外的文件'
  };

  function str(v) {
    if (v == null) return '';
    if (typeof v === 'string') return v;
    try { return JSON.stringify(v); } catch (e) { return String(v); }
  }

  function clip(s, n) {
    s = str(s).replace(/\s+/g, ' ').trim();
    n = n || 80;
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
  }

  function firstLine(s) {
    var lines = str(s).split('\n');
    for (var i = 0; i < lines.length; i++) {
      var t = lines[i].trim();
      if (t && t.charAt(0) !== '#' && !/^(import|from)\s/.test(t)) return t;
    }
    return lines[0] || '';
  }

  function quoted(s) { s = clip(s, 60); return s ? '“' + s + '”' : ''; }
  function pathOf(a) { return str(a.path); }

  // 名字 → [平常的动作名, 取对象的函数, 覆盖时的动作名]
  /** GET /api/toolsets 的缓存（组的中文名）。纯函数里只读它；没有时用 TOOLSET_NAMES。 */
  var TOOLSET_CACHE = { list: null };

  var TOOLS = {
    list_dir: ['查看文件夹', function (a) { var p = str(a.path).trim(); return !p || p === '.' || p === './' ? '整个文件夹' : p; }],
    read_file: ['读取文件', pathOf],
    write_file: ['写入文件', pathOf, '覆盖文件', '新建文件'],
    edit_file: ['修改文件', pathOf],
    search_files: ['搜索文件', function (a) { return quoted(a.query) + (a.glob ? '（' + str(a.glob) + '）' : ''); }],
    move_file: ['移动文件', function (a) { return str(a.src) + ' → ' + str(a.dst); }],
    delete_file: ['删除文件', pathOf],
    excel_inspect: ['查看表格结构', pathOf],
    excel_read: ['读取表格', function (a) { return str(a.path) + (a.sheet ? ' / ' + str(a.sheet) : ''); }],
    excel_diff: ['对账两张表', function (a) { return str(a.path_a) + ' 与 ' + str(a.path_b); }, '对账并覆盖结果表'],
    write_xlsx: ['生成 Excel', pathOf, '覆盖 Excel'],
    write_docx: ['生成 Word 文档', pathOf, '覆盖 Word 文档'],
    write_pptx: ['生成 PPT', pathOf, '覆盖 PPT'],
    kb_add: ['导入资料库', pathOf],
    kb_search: ['在资料里找', function (a) { return quoted(a.query) + (a.library ? '（' + str(a.library) + '）' : ''); }],
    kb_open: ['打开资料', function (a) { return /^\d+$/.test(str(a.ref)) ? '出处 [' + str(a.ref) + ']' : str(a.ref); }],
    kb_list: ['看资料库里有什么', function (a) { return [str(a.library), a.query ? quoted(a.query) : ''].filter(Boolean).join(' '); }],
    pptx_outline: ['看 PPT 的结构', pathOf],
    run_python: ['运行分析代码', function (a) { return clip(firstLine(a.code), 60); }],
    web_fetch: ['打开网页', function (a) { return str(a.url); }],
    web_search: ['联网搜索', function (a) { return quoted(a.query); }],
    todo_write: ['更新计划', function (a) { return Array.isArray(a.items) ? a.items.length + ' 项' : ''; }],
    ask_user: ['向你提问', function (a) { return clip(a.question, 60); }],
    memory_save: ['记住一件事', function (a) { return quoted(a.text); }],
    memory: ['更新记忆', function (a) {
      var how = { add: '记下', replace: '改成', remove: '删掉' }[a.action] || '';
      var what = a.action === 'remove' ? a.old_text : (a.content || a.old_text);
      return what ? (how ? how + ' ' : '') + quoted(what) : '';
    }],
    session_search: ['查找以前的对话', function (a) { return a.query ? quoted(a.query) : a.session_id ? '对话 ' + str(a.session_id) : ''; }],
    skill_save: ['保存技能', function (a) { return str(a.name); }],
    schedule_create: ['创建定时任务', function (a) { return str(a.when); }],
    skill_load: ['读取技能说明', function (a) { return str(a.name); }],
    system_info: ['查看电脑配置', function () { return ''; }],
    spawn_agent: ['派助手去调查', function (a) { return clip(a.task, 60); }],
    // 第 3.4 节：按需加载工具。步骤行写成"加载工具：操作网页"（名字整句放在 verb 里，见 toolInfo）
    tool_load: ['加载工具', function () { return ''; }],
    // 操作网页、操作桌面：后端会给 display（见 displayOf），这几项只在没有 display 时（以及设置页的动作表）用
    browser_open: ['在浏览器里打开', function (a) { return str(a.url); }],
    browser_look: ['看网页', function (a) { return a.find ? quoted(a.find) : ''; }],
    browser_do: ['操作网页', function (a) { return [str(a.action), a.ref ? str(a.ref) : '', a.text ? quoted(a.text) : ''].filter(Boolean).join(' '); }],
    desktop_open: ['打开桌面程序', function (a) { return str(a.app); }],
    desktop_look: ['看桌面程序', function () { return ''; }],
    desktop_do: ['操作桌面程序', function (a) { return [str(a.action), str(a.target), a.text ? quoted(a.text) : ''].filter(Boolean).join(' '); }]
  };

  /** 工具调用 → 人话。返回 {verb, target, hidden}。 */
  function toolInfo(name, args, risk) {
    var a = parseArgs(args);
    name = str(name);
    var t = TOOLS[name];
    if (t) {
      var verb = t[0];
      if (risk === 'overwrite' && t[2]) verb = t[2];
      else if (risk === 'create' && t[3]) verb = t[3];
      var target = '';
      try { target = t[1](a); } catch (e) { target = ''; }
      if (name === 'tool_load') {
        var gs = Array.isArray(a.groups) ? a.groups : a.group != null ? [a.group] : [];
        var gn = gs.map(function (g) { return toolsetName(g, TOOLSET_CACHE.list); }).filter(Boolean).join('、');
        return { verb: gn ? '加载工具：' + gn : '加载工具', target: '', hidden: false };
      }
      return { verb: verb, target: clip(target, 120), hidden: name === 'todo_write' || name === 'ask_user' };
    }
    var mm = /^mcp_(.+)$/.exec(name);
    if (mm) {
      return { verb: '调用外部系统', target: mm[1].replace(/_/g, ' '), hidden: false };
    }
    return { verb: '使用工具', target: name, hidden: false };
  }

  function parseArgs(x) {
    if (x && typeof x === 'object' && !Array.isArray(x)) return x;
    if (typeof x === 'string' && x.trim()) {
      try { var v = JSON.parse(x); return v && typeof v === 'object' && !Array.isArray(v) ? v : {}; } catch (e) { return {}; }
    }
    return {};
  }

  function riskLabel(r) { return RISK_LABEL[r] || (r ? String(r) : '未知'); }

  /* ---------------- 状态文字 ---------------- */

  var STATUS_TEXT = {
    idle: '未开始', running: '运行中', waiting_approval: '等你批准', waiting_input: '等你回答',
    paused: '已暂停', done: '已完成', failed: '出错', cancelled: '已取消'
  };

  function statusText(s) { return STATUS_TEXT[s] || (s ? String(s) : '未开始'); }
  function needsAttention(s) { return s === 'waiting_approval' || s === 'waiting_input' || s === 'paused'; }
  function isActive(s) { return s === 'running' || s === 'waiting_approval' || s === 'waiting_input'; }

  /* ---------------- 事件 → 任务视图（归并） ----------------
   * view 是一个普通对象；reduceEvent 会就地修改并返回它（流式输出时避免反复复制）。
   * 每个时间线条目有稳定的 key 和版本号 v，界面按 key 增量更新。
   */

  function newView(session) {
    session = session || {};
    return {
      sid: session.id || null,
      mode: str(session.mode).toLowerCase(),   // 对话属于哪个模式；老后端没有，是 ''
      hits: {},           // 出处编号 → 命中的片段（kb_search 的 hits），整个对话里编号不变
      grounding: {},      // 回答的事件序号 → 核对数字的结果（grounding 事件）
      feedback: {},       // 回答的事件序号 → {rating, note}（feedback 事件）
      title: session.title || '',
      status: session.status || 'idle',
      reason: (session.meta && session.meta.reason) || '',
      revision: session.revision || 1,
      todo: (session.meta && Array.isArray(session.meta.todo)) ? session.meta.todo : [],
      libraries: (session.meta && Array.isArray(session.meta.libraries)) ? session.meta.libraries.map(str) : [],
      docs: (session.meta && Array.isArray(session.meta.docs)) ? session.meta.docs.map(str) : [],     // 第 2.4 节：指定文件
      tags: (session.meta && Array.isArray(session.meta.tags)) ? session.meta.tags.map(str) : [],     // 按标签
      items: [],
      byCall: {},
      artifacts: {},      // 路径 → [{id, version, note, ts}]，新版本在后
      lastSeq: 0,
      steps: 0,
      endpoint: '',
      streamN: 0,
      undoneLogs: {}      // 已撤销的记忆改动编号（memory_undo 可能先于对应的 memory 事件到达）
    };
  }

  function touch(it) { it.v = (it.v || 0) + 1; return it; }

  function findStream(view) {
    for (var i = view.items.length - 1; i >= 0; i--) {
      if (view.items[i].kind === 'assistant' && view.items[i].streaming) return i;
    }
    return -1;
  }

  function dropStream(view) {
    var i = findStream(view);
    if (i >= 0) view.items.splice(i, 1);
  }

  function toolItem(view, callId, tool, args) {
    var it = callId ? view.byCall[callId] : null;
    if (!it) {
      it = { key: 'c' + (callId || ('x' + view.items.length)), kind: 'tool', callId: callId || '', tool: str(tool),
             args: parseArgs(args), rawArgs: args, state: 'planned', v: 0 };
      view.items.push(it);
      if (callId) view.byCall[callId] = it;
    }
    return it;
  }

  function findAction(view, actionId) {
    if (!actionId) return null;
    for (var i = 0; i < view.items.length; i++) {
      if (view.items[i].kind === 'tool' && view.items[i].actionId === actionId) return view.items[i];
    }
    return null;
  }

  function addArtifact(view, p, ts) {
    if (!p || !p.path) return;
    var list = view.artifacts[p.path] || (view.artifacts[p.path] = []);
    for (var i = 0; i < list.length; i++) if (list[i].id === p.id && p.id) return;
    list.push({ id: p.id || '', path: p.path, version: p.version || list.length + 1, note: p.note || '', ts: ts || 0 });
    list.sort(function (a, b) { return a.version - b.version; });
  }

  function reduceEvent(view, ev) {
    if (!ev || typeof ev !== 'object') return view;
    var seq = typeof ev.seq === 'number' ? ev.seq : null;
    if (seq != null) {
      if (seq <= view.lastSeq) return view;   // 重连补发时的重复事件
      view.lastSeq = seq;
    }
    var p = ev.payload && typeof ev.payload === 'object' ? ev.payload : {};
    var key = seq != null ? 'e' + seq : 'n' + view.items.length + '_' + (ev.type || '');
    var it;
    switch (ev.type) {
      case 'user_message':
        dropStream(view);
        view.items.push({ key: key, kind: 'user', text: str(p.text), images: Array.isArray(p.images) ? p.images : [],
                          files: normalizeFiles(p.files), skill: p.skill ? str(p.skill) : '', deep: p.depth === 'deep', ts: ev.ts, v: 0 });
        if (!view.title && p.text) view.title = clip(p.text, 20);
        break;

      case 'assistant_delta': {
        // 片段没有序号。它带着"那次模型调用开始时账本写到第几条"（base）：如果这次调用的最终回答
        // 已经在视图里了（装载对话时先拿到了账本、缓冲里的片段后到），这些片段就是过时的，丢掉，
        // 否则同一段回答会显示两遍
        if (p.base != null && view.asstSeq != null && view.asstSeq > Number(p.base)) break;
        var si = findStream(view);
        if (si < 0) {
          view.streamN++;
          view.items.push({ key: 's' + view.streamN + '_' + view.lastSeq, kind: 'assistant', text: '', streaming: true, v: 0 });
          si = view.items.length - 1;
        }
        it = view.items[si];
        it.text += str(p.text);
        touch(it);
        break;
      }

      case 'assistant_message': {
        var content = str(p.content);
        var idx = findStream(view);
        if (seq != null) view.asstSeq = seq;
        if (stripThink(content).trim()) {
          if (idx >= 0) {
            it = view.items[idx];
            it.text = content; it.streaming = false; it.ts = ev.ts;
            it.calls = Array.isArray(p.tool_calls) ? p.tool_calls.length : 0;
            touch(it);
          } else {
            view.items.push({ key: key, kind: 'assistant', text: content, streaming: false, ts: ev.ts, v: 0,
                              calls: Array.isArray(p.tool_calls) ? p.tool_calls.length : 0 });
            it = view.items[view.items.length - 1];
          }
          if (seq != null) it.seq = seq;        // 核对结果、反馈按这个序号挂上来
        } else if (idx >= 0) {
          view.items.splice(idx, 1);
        }
        var calls = Array.isArray(p.tool_calls) ? p.tool_calls : [];
        calls.forEach(function (tc) {
          if (!tc || typeof tc !== 'object') return;
          var t = toolItem(view, tc.id, tc.name, tc.arguments);
          if (tc.error) t.parseError = str(tc.error);
          touch(t);
        });
        view.steps++;
        if (p.endpoint) view.endpoint = str(p.endpoint);
        break;
      }

      case 'approval_requested':
        it = toolItem(view, p.call_id, p.tool, p.args);
        it.state = 'awaiting';
        it.actionId = p.action_id || it.actionId;
        it.risk = p.risk || it.risk;
        if (p.args) it.args = parseArgs(p.args);
        if (p.display) it.display = mergeDisplay(it.display, p.display);
        touch(it);
        break;

      case 'approval_decided':
        it = findAction(view, p.action_id);
        if (it) {
          it.state = p.allow ? 'approved' : 'denied';
          touch(it);
        }
        break;

      case 'tool_started':
        it = toolItem(view, p.call_id, p.tool, p.args);
        it.state = 'running';
        it.actionId = p.action_id || it.actionId;
        it.startedAt = ev.ts;
        if (p.display) it.display = mergeDisplay(it.display, p.display);
        touch(it);
        break;

      case 'tool_result':
        it = toolItem(view, p.call_id, p.tool, null);
        var text = str(p.content);
        if (p.ok) it.state = 'done';
        else if (/^未执行/.test(text)) it.state = 'voided';
        else if (/^用户拒绝/.test(text)) it.state = 'denied';
        else if (/^此动作在执行中途程序中断/.test(text)) it.state = 'unknown';
        else it.state = 'failed';
        it.result = text;
        it.truncated = !!p.truncated;
        if (p.action_id) it.actionId = p.action_id;
        if (p.display) it.display = mergeDisplay(it.display, p.display);
        it.endedAt = ev.ts;
        // 资料检索：hits 收进对话的出处表（同一个编号始终指同一段原文）；这一步记下自己找到了哪几条
        if (Array.isArray(p.hits)) it.hits = collectHits(view, p.hits);
        touch(it);
        if (it.tool === 'ask_user') markAnswered(view, p.call_id, null);
        break;

      case 'artifact':
        addArtifact(view, p, ev.ts);
        view.items.push({ key: key, kind: 'artifact', path: str(p.path), version: p.version || 1, note: str(p.note), v: 0 });
        break;

      case 'todo':
        view.todo = Array.isArray(p.items) ? p.items : [];
        break;

      case 'question':
        view.items.push({ key: key, kind: 'question', callId: p.call_id || '', question: str(p.question),
                          options: Array.isArray(p.options) ? p.options.map(str) : [], answered: false, answer: '', v: 0 });
        break;

      case 'answer':
        markAnswered(view, p.call_id, str(p.text));
        break;

      case 'memory':
        // 新格式带 op、origin、log_id；旧格式只有 text，按"记住了"显示
        view.items.push({ key: key, kind: 'memory', op: p.op === 'replace' || p.op === 'remove' ? p.op : 'add',
                          origin: p.origin === 'review' ? 'review' : str(p.origin) || 'chat', text: str(p.text),
                          before: p.before == null ? '' : str(p.before), target: str(p.target),
                          logId: p.log_id == null || p.log_id === '' ? null : p.log_id,
                          undone: p.log_id != null && p.log_id !== '' && !!view.undoneLogs[str(p.log_id)], v: 0 });
        break;

      case 'memory_undo': {
        // 撤销成功后后端记的事件：把对应的记忆提示标成已撤销，本身不显示
        if (p.log_id == null || p.log_id === '') break;
        var lid = str(p.log_id);
        view.undoneLogs[lid] = true;
        view.items.forEach(function (x) {
          if (x.kind === 'memory' && x.logId != null && str(x.logId) === lid && !x.undone) { x.undone = true; touch(x); }
        });
        break;
      }

      case 'skill':
        view.items.push({ key: key, kind: 'skill', name: str(p.name), description: str(p.description), updated: !!p.updated, v: 0 });
        view.items.forEach(function (x) { if (x.kind === 'suggest' && x.what === 'save_skill' && !x.done) { x.done = true; touch(x); } });
        break;

      case 'suggestion':
        if (!p.text && p.kind !== 'save_skill') break;
        view.items.push({ key: key, kind: 'suggest', what: str(p.kind),
                          text: str(p.text) || '这次的做法可以存成技能，下次同类的事直接照做。', done: false, v: 0 });
        break;

      case 'status':
        view.status = p.status || view.status;
        view.reason = p.reason ? str(p.reason) : (view.status === 'paused' ? view.reason : '');
        if (view.status !== 'running') dropStream(view);
        break;

      case 'notice':
        view.items.push({ key: key, kind: 'note', tone: 'notice', text: str(p.text), v: 0 });
        break;

      case 'loop_guard': {
        // 第 3.8 节：同样的调用重复了，程序拦下了这一次。那一步标"重复的调用，已拦下"
        it = toolItem(view, p.call_id, p.tool, null);
        it.looped = Math.max(1, parseInt(p.count, 10) || 1);
        touch(it);
        break;
      }

      case 'grounding': {
        // 回答说完之后程序核对数字的结果（规格 9.3）：按 msg_seq 挂到那条回答上
        var g = normGrounding(p), ms = parseInt(p.msg_seq, 10);
        if (!g || !(ms > 0)) break;
        view.grounding[ms] = g;
        view.items.forEach(function (x) { if (x.kind === 'assistant' && x.seq === ms) touch(x); });
        break;
      }

      case 'feedback': {
        var fb = normFeedback(p);
        if (!fb || !fb.seq) break;
        view.feedback[fb.seq] = { rating: fb.rating, note: fb.note };
        view.items.forEach(function (x) { if (x.kind === 'assistant' && x.seq === fb.seq) touch(x); });
        break;
      }

      case 'error':
        dropStream(view);
        view.items.push({ key: key, kind: 'note', tone: 'error', text: str(p.text), v: 0 });
        break;

      case 'summary':
        view.items.push({ key: key, kind: 'summary', text: str(p.text), v: 0 });
        break;

      case 'compact':
        // 手动整理（/整理）：started 只推送不落账；done / failed 收掉"正在整理"，失败时留一行
        if (p.state === 'started') view.compacting = true;
        else if (p.state === 'done' || p.state === 'failed') {
          view.compacting = false;
          if (p.state === 'failed') view.items.push({ key: key, kind: 'note', tone: 'error', text: str(p.error) || '整理失败。', v: 0 });
        }
        break;

      default:
        break;
    }
    return view;
  }

  function markAnswered(view, callId, text) {
    for (var i = view.items.length - 1; i >= 0; i--) {
      var q = view.items[i];
      if (q.kind === 'question' && !q.answered && (!callId || !q.callId || q.callId === callId)) {
        q.answered = true;
        if (text != null) q.answer = text;
        touch(q);
        return;
      }
    }
  }

  /** 用 GET /api/sessions/<id> 里的 pending（等批准的动作）补齐视图。 */
  function applyPending(view, pending) {
    if (!Array.isArray(pending)) return view;
    pending.forEach(function (a) {
      if (!a || typeof a !== 'object') return;
      var it = toolItem(view, a.call_id, a.tool, a.args);
      if (it.state === 'done' || it.state === 'failed' || it.state === 'voided' || it.state === 'denied') return;
      it.state = 'awaiting';
      it.actionId = a.id || a.action_id || it.actionId;
      it.risk = a.risk || it.risk;
      if (a.args) it.args = parseArgs(a.args);
      if (a.display) it.display = mergeDisplay(it.display, a.display);
      touch(it);
    });
    return view;
  }

  function buildView(session, events, pending, artifacts) {
    var v = newView(session);
    (events || []).forEach(function (e) { reduceEvent(v, e); });
    if (session && session.status) v.status = session.status;
    if (session && session.meta && session.meta.reason) v.reason = session.meta.reason;
    if (session && session.title) v.title = session.title;
    applyPending(v, pending);
    (artifacts || []).forEach(function (a) { if (a) addArtifact(v, a, a.created); });
    return v;
  }

  /** 现在需要用户做的事。 */
  function attention(view) {
    var approvals = [], question = null;
    view.items.forEach(function (it) {
      if (it.kind === 'tool' && it.state === 'awaiting') approvals.push(it);
      if (it.kind === 'question' && !it.answered) question = it;
    });
    if (view.status !== 'waiting_input') question = null;
    return { approvals: approvals, question: question, paused: view.status === 'paused' || view.status === 'failed' };
  }

  /* ---------------- 任务列表 ---------------- */

  /** 把一条事件应用到任务列表上。返回 {changed, unknown}，unknown 表示列表里没有这个任务（需要重新拉列表）。 */
  function applyListEvent(sessions, ev) {
    var res = { changed: false, unknown: false };
    if (!ev || !Array.isArray(sessions)) return res;
    var sid = ev.session_id || ev.sid;
    if (!sid) return res;
    var s = null;
    for (var i = 0; i < sessions.length; i++) if (sessions[i].id === sid) { s = sessions[i]; break; }
    if (!s) {
      res.unknown = ev.type === 'user_message' || ev.type === 'status';
      return res;
    }
    var p = ev.payload || {};
    if (ev.type === 'status' && p.status && p.status !== s.status) {
      s.status = p.status;
      if (!s.meta || typeof s.meta !== 'object') s.meta = {};
      s.meta.reason = p.reason || '';
      res.changed = true;
    }
    // 后台回顾记下的东西不算任务有新进展，免得它跳到列表顶上
    var quiet = ev.type === 'assistant_delta' || ev.type === 'suggestion' || ev.type === 'memory_undo' ||
      (ev.type === 'memory' && p.origin === 'review');
    if (ev.ts && !quiet && (!s.updated || ev.ts > s.updated)) {
      s.updated = ev.ts;
      res.changed = true;
    }
    if (res.changed) sessions.sort(function (a, b) { return (b.updated || 0) - (a.updated || 0); });
    return res;
  }

  /* ---------------- 小工具 ---------------- */

  function groupTemplates(list) {
    var groups = [], idx = {};
    (Array.isArray(list) ? list : []).forEach(function (t) {
      if (!t || !t.prompt) return;
      var role = str(t.role) || '通用';
      if (!(role in idx)) { idx[role] = groups.length; groups.push({ role: role, items: [] }); }
      // 第 6 节：id、params、source 原样带着（带参数的点了弹表单；"我的模板"按 source 认）
      groups[idx[role]].items.push({ title: str(t.title) || clip(t.prompt, 24), prompt: str(t.prompt),
        id: t.id != null ? str(t.id) : '', params: Array.isArray(t.params) ? t.params : null, source: str(t.source), mode: str(t.mode), role: role });
    });
    return groups;
  }

  function pad(n) { return n < 10 ? '0' + n : '' + n; }

  /** ts 是秒（后端 time.time()）。 */
  function formatTime(ts, nowMs) {
    if (!ts) return '';
    var d = new Date(ts * 1000);
    if (isNaN(d.getTime())) return '';
    var now = new Date(nowMs || Date.now());
    var hm = pad(d.getHours()) + ':' + pad(d.getMinutes());
    if (d.toDateString() === now.toDateString()) return '今天 ' + hm;
    var y = new Date(now.getTime() - 86400000);
    if (d.toDateString() === y.toDateString()) return '昨天 ' + hm;
    var md = (d.getMonth() + 1) + '月' + d.getDate() + '日 ' + hm;
    return d.getFullYear() === now.getFullYear() ? md : d.getFullYear() + '年' + md;
  }

  function relativeTime(ts, nowMs) {
    if (!ts) return '';
    var diff = ((nowMs || Date.now()) / 1000) - ts;
    if (diff < 60) return '刚刚';
    if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
    if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
    if (diff < 86400 * 7) return Math.floor(diff / 86400) + ' 天前';
    var d = new Date(ts * 1000);
    return (d.getMonth() + 1) + '月' + d.getDate() + '日';
  }

  var WEEKDAY = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

  function dayStart(ms) { var d = new Date(ms); d.setHours(0, 0, 0, 0); return d.getTime(); }

  /** 侧栏对话列表按时间分组：今天、昨天、近 7 天、更早。sessions 已按更新时间排好。返回 [{label, items}]。 */
  function groupSessions(sessions, nowMs) {
    var now = nowMs || Date.now(), today = dayStart(now), out = [], idx = {};
    (Array.isArray(sessions) ? sessions : []).forEach(function (s) {
      if (!s) return;
      var ms = (Number(s.updated) || 0) * 1000;
      var label = !ms ? '更早' : ms >= today ? '今天' : ms >= today - 86400000 ? '昨天' : ms >= today - 6 * 86400000 ? '近 7 天' : '更早';
      if (!(label in idx)) { idx[label] = out.length; out.push({ label: label, items: [] }); }
      out[idx[label]].items.push(s);
    });
    var order = ['今天', '昨天', '近 7 天', '更早'];
    return out.sort(function (a, b) { return order.indexOf(a.label) - order.indexOf(b.label); });
  }

  /** 侧栏每一项右边的时间：今天、昨天显示几点，近 7 天显示星期几，更早显示几月几日。 */
  function listTime(ts, nowMs) {
    if (!ts) return '';
    var ms = ts * 1000, d = new Date(ms);
    if (isNaN(d.getTime())) return '';
    var today = dayStart(nowMs || Date.now());
    if (ms >= today - 86400000) return pad(d.getHours()) + ':' + pad(d.getMinutes());
    if (ms >= today - 6 * 86400000) return WEEKDAY[d.getDay()];
    return (d.getMonth() + 1) + '月' + d.getDate() + '日';
  }

  /** 步骤用了多久："40 秒"、"1 分 12 秒"、"1 小时 3 分"。 */
  function formatElapsed(sec) {
    if (typeof sec !== 'number' || !isFinite(sec) || sec < 0) return '';
    sec = Math.round(sec);
    if (sec < 60) return sec + ' 秒';
    if (sec < 3600) return Math.floor(sec / 60) + ' 分' + (sec % 60 ? ' ' + (sec % 60) + ' 秒' : '');
    var m = Math.floor((sec % 3600) / 60);
    return Math.floor(sec / 3600) + ' 小时' + (m ? ' ' + m + ' 分' : '');
  }

  /** 新对话页的问候：按几点换。 */
  function greeting(hour) {
    if (hour >= 5 && hour < 12) return '上午好';
    if (hour >= 12 && hour < 18) return '下午好';
    return '晚上好';
  }

  function formatSize(n) {
    if (n == null || isNaN(n)) return '';
    n = Number(n);
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(n < 10240 ? 1 : 0) + ' KB';
    return (n / 1024 / 1024).toFixed(1) + ' MB';
  }

  function fileName(p) {
    var s = str(p).replace(/\\/g, '/').replace(/\/+$/, '');
    var i = s.lastIndexOf('/');
    return i >= 0 ? s.slice(i + 1) : s;
  }

  /** 结果文字如果是 JSON，就排版一下，方便看。 */
  function prettyResult(s) {
    s = str(s);
    var t = s.trim();
    if ((t.charAt(0) === '{' || t.charAt(0) === '[') && t.length < 200000) {
      try { return JSON.stringify(JSON.parse(t), null, 2); } catch (e) { /* 不是 JSON */ }
    }
    return s;
  }

  /** 命令面板的匹配：连续子串优先，其次按顺序出现的字符。 */
  function matchScore(query, text) {
    var q = str(query).toLowerCase().replace(/\s+/g, '');
    var t = str(text).toLowerCase();
    if (!q) return 1;
    var i = t.indexOf(q);
    if (i >= 0) return 100 - Math.min(i, 50);
    var pos = 0;
    for (var k = 0; k < q.length; k++) {
      pos = t.indexOf(q.charAt(k), pos);
      if (pos < 0) return 0;
      pos++;
    }
    return 10;
  }

  function filterCommands(items, query) {
    return (items || []).map(function (it, i) {
      return { it: it, i: i, s: matchScore(query, (it.label || '') + ' ' + (it.keywords || '') + ' ' + (it.hint || '')) };
    }).filter(function (x) { return x.s > 0; })
      .sort(function (a, b) { return b.s - a.s || a.i - b.i; })
      .map(function (x) { return x.it; });
  }

  /* ---------------- 斜杠指令（纯逻辑） ----------------
   * 输入框第一个字符是 / ／ 、（中文输入法按 / 键常出这两个）时当指令开头。
   * when：any 任何时候；session 打开了对话；running 正在做；stopped 停下或出错。
   * arg：参数提示；argNeed 为真时必须写参数；pick 为真时不带参数就在菜单里列出可选项。
   */
  var SLASH_COMMANDS = [
    { id: 'new', name: '新对话', alias: 'new', desc: '开一个新对话', py: 'xinduihua xdh', when: 'any' },
    { id: 'stop', name: '停止', alias: 'stop', desc: '停下正在做的事', py: 'tingzhi tz', when: 'running' },
    { id: 'continue', name: '继续', alias: 'continue', desc: '接着做', py: 'jixu jx', when: 'stopped' },
    { id: 'compact', name: '整理', alias: 'compact', desc: '把较早的过程压成摘要，腾出上下文', arg: '[要保留的重点]', py: 'zhengli zl', when: 'idle' },
    { id: 'rename', name: '改名', alias: 'rename', desc: '改这个对话的标题', arg: '<新标题>', argNeed: true, py: 'gaiming gm', when: 'session' },
    { id: 'export', name: '导出', alias: 'export', desc: '导出对话记录', py: 'daochu dc', when: 'session' },
    { id: 'delete', name: '删除', alias: 'delete', desc: '删除这个对话（先确认）', py: 'shanchu sc', when: 'session' },
    { id: 'search', name: '搜索', alias: 'search', desc: '搜以前的对话', arg: '<词>', argNeed: true, py: 'sousuo ss', when: 'any' },
    { id: 'remember', name: '记住', alias: 'remember', desc: '直接记进"关于我"', arg: '<内容>', argNeed: true, py: 'jizhu jz', when: 'any' },
    { id: 'memory', name: '记忆', alias: 'memory', desc: '打开记忆页', py: 'jiyi jy', when: 'any' },
    { id: 'skills', name: '技能', alias: 'skills', desc: '打开技能页', py: 'jineng jn', when: 'any' },
    { id: 'save-skill', name: '存成技能', alias: 'save-skill', desc: '把这次的做法存成技能', py: 'cunchengjineng ccjn', when: 'session' },
    { id: 'file', name: '文件', alias: 'file', desc: '添加文件', py: 'wenjian wj', when: 'any' },
    { id: 'folder', name: '文件夹', alias: 'folder', desc: '用一个文件夹工作', py: 'wenjianjia wjj', when: 'any' },
    { id: 'mode', name: '批准', alias: 'mode', desc: '改批准方式', arg: '[每步都问|自动|全放行]', pick: true, py: 'pizhun pz', when: 'any' },
    { id: 'theme', name: '主题', alias: 'theme', desc: '换主题', arg: '[主题名]', pick: true, py: 'zhuti zt', when: 'any' },
    { id: 'status', name: '状态', alias: 'status', desc: '模型、内核、上下文和记忆用量', py: 'zhuangtai zt', when: 'any' },
    { id: 'schedule', name: '定时', alias: 'schedule', desc: '打开定时页', py: 'dingshi ds', when: 'any' },
    { id: 'kb', name: '资料', alias: 'kb', desc: '打开资料页', py: 'ziliao zl', when: 'any', space: [''] },
    { id: 'library', name: '知识库', alias: 'library', desc: '打开知识库页：资料库、检索测试、标准答案', py: 'zhishiku zsk', when: 'any', space: ['cariai', 'hspace'] },
    { id: 'scope', name: '范围', alias: 'scope', desc: '选这次查哪些资料库', arg: '[资料库名|全部]', py: 'fanwei fw', when: 'any', space: ['hspace'] },
    { id: 'deep', name: '深入', alias: 'deep', desc: '深入研究：拆成小问题、多种问法查，写成有结构的回答', arg: '<问题>', argNeed: true, py: 'shenru sr', when: 'idle-or-new', space: ['hspace'] },
    { id: 'settings', name: '设置', alias: 'settings', desc: '打开设置页', py: 'shezhi sz', when: 'any' },
    { id: 'help', name: '帮助', alias: 'help', desc: '列出全部指令', py: 'bangzhu bz', when: 'any' }
  ];

  var SLASH_CHARS = '/／、';

  /** 输入框的值 → 指令的样子。不是指令开头时返回 null。
   *  {name: 指令名（可能为空）, arg: 参数（去掉首尾空白）, spaced: 名字后面有没有空白（在写参数了）} */
  function parseSlash(value) {
    var v = str(value);
    if (!v || SLASH_CHARS.indexOf(v.charAt(0)) < 0) return null;
    var rest = v.slice(1);
    var m = /^(\S*)(\s[\s\S]*)?$/.exec(rest);
    var name = m ? m[1] : '';
    var tail = m && m[2] ? m[2] : '';
    return { name: name, arg: tail.trim(), spaced: tail.length > 0 };
  }

  /** 这个指令在当前状态下有没有意义。ctx = {session: 打开了对话, status: 对话状态, mode: 工作模式（老后端为 ''）}
   *  cmd.space：只在这几个模式里有（'' 表示老后端）；不写就是哪里都有。 */
  function slashAvailable(cmd, ctx) {
    ctx = ctx || {};
    if (Array.isArray(cmd.space) && cmd.space.indexOf(str(ctx.mode)) < 0) return false;
    var w = cmd.when || 'any';
    if (w === 'idle-or-new') return !ctx.session || !isActive(ctx.status || 'idle');
    if (w === 'any') return true;
    if (!ctx.session) return false;
    var st = ctx.status || 'idle';
    if (w === 'running') return isActive(st);
    if (w === 'stopped') return st === 'paused' || st === 'failed';
    if (w === 'session') return true;
    if (w === 'idle') return !isActive(st);
    return false;
  }

  /** 全部可用的指令：内置（按状态过滤）+ 技能 + 模板。每项 {group: cmd|skill|tpl, name, alias, desc, arg, …} */
  function slashCommands(ctx, skills, templates) {
    var out = [];
    SLASH_COMMANDS.forEach(function (c) {
      if (slashAvailable(c, ctx)) out.push(Object.assign({ group: 'cmd' }, c));
    });
    var seen = {};
    out.forEach(function (c) { seen[c.name.toLowerCase()] = 1; seen[c.alias.toLowerCase()] = 1; });
    (Array.isArray(skills) ? skills : []).forEach(function (s) {
      var n = s && str(s.name);
      if (!n || /\s/.test(n) || seen[n.toLowerCase()]) return;   // 和内置指令重名的技能不做成指令，免得抢掉内置的
      seen[n.toLowerCase()] = 1;
      out.push({ group: 'skill', id: 'skill', name: n, alias: '', desc: (s.source === 'shared' ? '公司 · ' : '') + (str(s.description) || '技能'), arg: '[要求]', skill: n, source: str(s.source) });
    });
    (Array.isArray(templates) ? templates : []).forEach(function (g) {
      (g && Array.isArray(g.items) ? g.items : []).forEach(function (t) {
        var n = str(t.title).replace(/\s+/g, '');
        if (!n || seen[n.toLowerCase()]) return;
        seen[n.toLowerCase()] = 1;
        out.push({ group: 'tpl', id: 'tpl', name: n, alias: '', desc: clip(t.prompt, 60), prompt: str(t.prompt), role: str(g.role), tpl: t,
                   arg: tplParams(t).length ? '（填几个空）' : '' });
      });
    });
    return out;
  }

  /** 按输入筛选：中文名、英文别名优先，说明里的字也算（只认连续出现）。同组的排在一起，组内按匹配程度排。 */
  function filterSlash(list, query) {
    var q = str(query).toLowerCase().replace(/\s+/g, '');
    var scored = (list || []).map(function (c, i) {
      if (!q) return { c: c, i: i, s: 1 };
      var n = c.name.toLowerCase(), a = str(c.alias).toLowerCase();
      var s = 0;
      if (n === q || a === q) s = 300;
      else if (n.indexOf(q) === 0 || (a && a.indexOf(q) === 0)) s = 200;
      else s = Math.max(matchScore(q, n), a ? matchScore(q, a) : 0);
      // 拼音全拼和首字母（只有内置指令有）：排在中文名和英文别名之后
      if (s < 200 && c.py) {
        var ps = 0;
        str(c.py).split(' ').forEach(function (p) {
          if (p === q) ps = Math.max(ps, 150);
          else if (p.indexOf(q) === 0) ps = Math.max(ps, 120);
        });
        s = Math.max(s, ps);
      }
      if (!s && str(c.desc).toLowerCase().indexOf(q) >= 0) s = 5;
      return { c: c, i: i, s: s };
    }).filter(function (x) { return x.s > 0; });
    // 组的顺序：组里最好的匹配越好越靠前；一样好时按 指令、技能、模板
    var order = { cmd: 0, skill: 1, tpl: 2 }, best = {};
    scored.forEach(function (x) { best[x.c.group] = Math.max(best[x.c.group] || 0, x.s); });
    scored.sort(function (a, b) {
      return (best[b.c.group] - best[a.c.group]) || (order[a.c.group] || 0) - (order[b.c.group] || 0) || b.s - a.s || a.i - b.i;
    });
    return scored.map(function (x) { return x.c; });
  }

  /** 名字（中文名或别名，不分大小写）完全对上的指令；找不到返回 null。 */
  function findSlash(list, name) {
    var n = str(name).toLowerCase();
    if (!n) return null;
    for (var i = 0; i < (list || []).length; i++) {
      var c = list[i];
      if (c.name.toLowerCase() === n || (c.alias && c.alias.toLowerCase() === n)) return c;
    }
    return null;
  }

  /** 回车时输入框里的指令要做什么。
   *  → null：不是指令开头，照常发送
   *  → {kind:'unknown'}：没有这个指令，当普通消息发
   *  → {kind:'unavailable', cmd}：有这个指令，但现在用不了（比如没在运行时 /停止）
   *  → {kind:'need-arg', cmd}：要写参数
   *  → {kind:'run', cmd, arg} */
  function resolveSlash(value, list) {
    var p = parseSlash(value);
    if (!p) return null;
    var cmd = findSlash(list, p.name);
    if (!cmd) {
      var builtin = findSlash(SLASH_COMMANDS, p.name);
      return builtin ? { kind: 'unavailable', cmd: builtin, arg: p.arg } : { kind: 'unknown' };
    }
    if (cmd.argNeed && !p.arg) return { kind: 'need-arg', cmd: cmd, arg: '' };
    return { kind: 'run', cmd: cmd, arg: p.arg };
  }

  /** 确认菜单里的一项后，输入框里该放的文字；返回 null 表示直接执行。 */
  function slashFill(cmd) {
    if (!cmd) return null;
    if (cmd.group === 'tpl') return cmd.prompt;
    if (cmd.group === 'skill' || cmd.arg) return '/' + cmd.name + ' ';
    return null;
  }

  /** 发消息的请求体。skill 有值时带上 "skill"（文字可以为空）。 */
  function messageBody(text, opts) {
    opts = opts || {};
    var body = { text: str(text) };
    if (opts.images && opts.images.length) body.images = opts.images;
    if (opts.files && opts.files.length) body.files = opts.files;
    if (opts.skill) body.skill = str(opts.skill);
    return body;
  }

  /** 批准方式：按中文名或 id 找。 */
  function matchMode(modes, arg) {
    var a = str(arg).trim().toLowerCase();
    if (!a) return null;
    for (var i = 0; i < (modes || []).length; i++) {
      if (modes[i].id === a || modes[i].label.toLowerCase() === a) return modes[i];
    }
    var hits = (modes || []).filter(function (m) { return m.label.indexOf(a) >= 0; });
    return hits.length === 1 ? hits[0] : null;
  }

  /** 主题：按名字或 id 找。"跟随系统""auto" → 'auto'。找不到返回 null。 */
  function matchTheme(list, arg) {
    var a = str(arg).trim().toLowerCase().replace(/\s+/g, ' ');
    if (!a) return null;
    if (a === 'auto' || a === '跟随系统' || a === '系统' || a === '自动') return 'auto';
    var flat = a.replace(/[\s_]+/g, '-');
    var arr = Array.isArray(list) ? list : [];
    for (var i = 0; i < arr.length; i++) {
      var t = arr[i] || {};
      if (str(t.id).toLowerCase() === flat || str(t.name).toLowerCase() === a) return str(t.id);
    }
    var hits = arr.filter(function (t) { return str(t.name).toLowerCase().indexOf(a) >= 0 || str(t.id).toLowerCase().indexOf(flat) >= 0; });
    return hits.length === 1 ? str(hits[0].id) : null;
  }

  /** /整理 的返回 → 一句话。 */
  function compactText(r) {
    if (r && r.started) return '开始整理较早的内容，好了会在对话里显示。';
    if (r && r.compacted === false) return '现在没有需要整理的。';
    return '已整理较早的内容。';
  }

  /** 千位分隔。 */
  function groupNum(n) {
    if (typeof n !== 'number' || !isFinite(n)) return '';
    return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  function usedOf(u) {
    if (!u || typeof u !== 'object' || typeof u.used !== 'number') return '';
    var lim = typeof u.limit === 'number' ? u.limit : (typeof u.budget === 'number' ? u.budget : null);
    if (!lim) return groupNum(u.used);
    return groupNum(u.used) + ' / ' + groupNum(lim) + '（' + Math.min(999, Math.round(u.used / lim * 100)) + '%）';
  }

  /** /状态 卡片的行：[{label, value, mono}]。st 是 /status 的返回（没打开对话时为 null），info 是 /api/state。 */
  function statusRows(st, info, modeLabel, curMode) {
    st = st && typeof st === 'object' ? st : {};
    info = info && typeof info === 'object' ? info : {};
    var rows = [];
    // 有两种模式时第一行写现在在哪个模式（对话的模式优先）
    var modes = normalizeModes(info);
    if (modes.length) {
      var wm = findModeById(modes, str(st.mode).toLowerCase() || curMode || '') || modes[0];
      rows.push({ label: '模式', value: wm.name });
    }
    var model = str(st.model || info.model);
    // 新版后端：对话最近一次实际用的端点和模型，显示成"端点名 · 模型名"
    var epName = str(st.endpoint);
    rows.push({ label: '模型', value: (epName && model ? epName + ' · ' + model : model || epName) || '未知' });
    // 内核只写名字；过渡内核时下面一行小字指到设置页。给开发者看的原话（kernel_notice）只放在设置 → 高级
    var kn = st.kernel || info.kernel;
    rows.push(kn === 'langgraph' ? { label: '内核', value: 'LangGraph' }
      : { label: '内核', value: kernelLabel(kn), hint: kn === 'builtin' || !kn ? '详情见 设置 → 高级' : '' });
    if (st.context) rows.push({ label: '上下文', value: usedOf(st.context) || '未知', mono: true });
    if (typeof st.steps === 'number') rows.push({ label: '已做步数', value: groupNum(st.steps), mono: true });
    var mem = st.memory && typeof st.memory === 'object' ? st.memory : (info.memory && typeof info.memory === 'object' ? info.memory : null);
    if (mem) {
      if (mem.user) rows.push({ label: '关于我', value: usedOf(mem.user) || '未知', mono: true });
      if (mem.workspace) rows.push({ label: '本文件夹笔记', value: usedOf(mem.workspace) || '未知', mono: true });
    }
    if (st.approval_mode) rows.push({ label: '批准方式', value: modeLabel ? modeLabel(st.approval_mode) : str(st.approval_mode) });
    // 第 3.9 节：工具按需加载。带了几组、多少字符；展开看各组（showStatus 里画）
    var ts = normToolset(st.toolset);
    if (ts) rows.push({ label: '工具', value: toolsetLine(ts), toolset: ts });
    // 新版后端在对话状态里带 browser：{open, url, title}
    if (st.browser && typeof st.browser === 'object') {
      rows.push(st.browser.open
        ? { label: '浏览器', value: '开着' + (st.browser.title ? '：' + clip(st.browser.title, 40) : ''), hint: clip(st.browser.url, 80) }
        : { label: '浏览器', value: '没开' });
    }
    return rows;
  }

  /* ---------------- 模型能力和功能开关（GET /api/capabilities） ----------------
   * 不可用的原因一律用后端给的 reason 原话，界面不另写说法。
   * 老后端没有这个接口（404 / 405）时 capsFromError 给 {missing: true}：界面不显示"模型能力""能做的事"，什么都不置灰。
   */

  function triState(v) { return v === true ? true : v === false ? false : null; }
  function posNum(x) { return typeof x === 'number' && isFinite(x) && x > 0 ? Math.round(x) : null; }

  /** 一个模型（端点）的能力。pointing 是 "pixel" / "norm1000" / false / null。 */
  function normModel(m, i) {
    m = m && typeof m === 'object' ? m : {};
    var src = m.source && typeof m.source === 'object' ? m.source : {};
    var pointing = typeof m.pointing === 'string' && m.pointing ? m.pointing : m.pointing === true ? 'yes' : m.pointing === false ? false : null;
    function so(k, v) { return str(src[k]) || (v == null ? 'unknown' : ''); }
    return {
      name: str(m.name) || (i != null ? '模型 ' + (i + 1) : ''), model: str(m.model),
      alive: m.alive === true ? true : m.alive === false ? false : null,
      tools: triState(m.tools), vision: triState(m.vision), pointing: pointing,
      context: posNum(m.context), context_detected: posNum(m.context_detected),
      source: { tools: so('tools', m.tools), vision: so('vision', m.vision), pointing: so('pointing', m.pointing) },
      probed_at: typeof m.probed_at === 'number' ? m.probed_at : null
    };
  }

  /** 整理 /api/capabilities 的返回（规格 7.5：models 按优先顺序；1.4 的老形状只有 model，也认）。认不出的输入返回 null。 */
  function normalizeCaps(d) {
    if (!d || typeof d !== 'object' || Array.isArray(d)) return null;
    var models = (Array.isArray(d.models) ? d.models : []).filter(function (m) { return m && typeof m === 'object'; })
      .map(function (m, i) { return normModel(m, i); });
    var model = normModel(d.model && typeof d.model === 'object' ? d.model : (d.models && d.models[0]) || {}, null);
    if (!models.length && d.model && typeof d.model === 'object') {
      var one = normModel(d.model, 0);
      if (!str(d.model.name)) one.name = '';
      if (one.alive == null) one.alive = true;
      models = [one];
    }
    var features = [], byId = {};
    (Array.isArray(d.features) ? d.features : []).forEach(function (f) {
      if (!f || typeof f !== 'object' || !f.id) return;
      var x = { id: str(f.id), name: str(f.name) || str(f.id), ok: f.ok !== false, unknown: !!f.unknown,
                reason_kind: str(f.reason_kind), reason: f.ok === false ? str(f.reason) : '',
                via: str(f.via), fallback: f.ok === false ? str(f.fallback) : '' };
      if (byId[x.id]) return;
      byId[x.id] = x;
      features.push(x);
    });
    return { models: models, model: model, features: features, byId: byId, embedding: normEmbedding(d.embedding) };
  }

  /** 取能力失败时：老后端（没有这个接口）→ {missing: true}；别的错误 → null（先不显示，下次再取）。 */
  function capsFromError(e) {
    if (e && (e.missing || e.status === 404 || e.status === 405)) return { missing: true };
    return null;
  }

  /** 某个功能现在能不能用。caps 是 normalizeCaps 的结果（或 {missing}），flags 是 /api/state 的 features。
   * 返回 {ok, unknown, reason, fallback, name}。什么都不知道时当作能用（老后端、还没取到）。 */
  function featureGate(caps, flags, id) {
    var f = caps && caps.byId ? caps.byId[id] : null;
    if (f) return { ok: f.ok, unknown: f.unknown, reason: f.ok ? '' : f.reason, fallback: f.ok ? '' : f.fallback, name: f.name };
    if (!(caps && caps.missing) && flags && typeof flags === 'object' && flags[id] === false) {
      return { ok: false, unknown: false, reason: '', fallback: '', name: '' };
    }
    return { ok: true, unknown: false, reason: '', fallback: '', name: '' };
  }

  var CAP_SOURCE = { config: '配置里写的', probe: '检测到的' };

  function yesNo(v) { return v === true || (typeof v === 'string' && v) ? '是' : v === false ? '否' : '未知'; }

  /** 一项能力的来源 → 人话。 */
  function capSource(m, k) {
    var s = m && m.source ? m.source[k] : '';
    if (CAP_SOURCE[s]) return CAP_SOURCE[s];
    var v = m ? m[k] : null;
    if (v == null) return m && typeof m.probed_at === 'number' ? '没检测出来' : '还没检测';
    return '';
  }

  /** 检测到模型支持的上下文比现在设的大时，给一句提示；否则空串。m 是一个模型（端点）。 */
  function contextHint(m) {
    if (!m || typeof m !== 'object') return '';
    var c = m.context, d = m.context_detected;
    if (typeof c !== 'number' || typeof d !== 'number' || !(c > 0) || !(d > c)) return '';
    return '模型最多支持 ' + groupNum(d) + '，现在设的是 ' + groupNum(c);
  }

  /** 设置页"模型能力"表：每个模型一行。cells 的 value 是 是 / 否 / 未知，source 是来源。 */
  function modelRows(caps) {
    var list = caps && Array.isArray(caps.models) ? caps.models : [];
    return list.map(function (m) {
      return {
        name: m.name, model: m.model,
        alive: m.alive === true ? '在线' : m.alive === false ? '掉线' : '未知', aliveOk: m.alive,
        cells: ['tools', 'vision', 'pointing'].map(function (k) { return { id: k, value: yesNo(m[k]), source: capSource(m, k) }; }),
        context: m.context ? groupNum(m.context) : '未知',
        hint: contextHint(m)
      };
    });
  }

  /** 功能由哪个端点承担：不是排第一的那个时给"由 X 承担"，否则空串。 */
  function viaText(caps, f) {
    if (!f || !f.ok || !f.via) return '';
    var first = caps && caps.models && caps.models[0] ? caps.models[0].name : '';
    return f.via !== first ? '由 ' + f.via + ' 承担' : '';
  }

  /** 各功能的行（设置页和 /状态 卡片共用）：[{id, name, ok, unknown, text, fallback, via}]。不可用时 text 是后端的原话。 */
  function featureLines(caps) {
    if (!caps || !Array.isArray(caps.features)) return [];
    return caps.features.map(function (f) {
      return { id: f.id, name: f.name, ok: f.ok, unknown: f.ok && f.unknown, text: f.ok ? '' : (f.reason || f.name),
               fallback: f.ok ? '' : f.fallback, via: viaText(caps, f) };
    });
  }

  /* ---------------- 允许的网站名单 ---------------- */

  var DOMAIN_RE = /^[a-z0-9.-]{1,253}$/;

  /** 用户填的网站 → 域名。转小写、去空白和首尾的点；贴进来的是网址时只取域名（去掉协议、端口、路径）。
   * 返回 {ok, value, error}。 */
  function normalizeDomain(raw) {
    var s = str(raw).replace(/\s+/g, '').toLowerCase();
    var m = /^[a-z][a-z0-9+.-]*:\/\/([^\/?#]*)/.exec(s);
    if (m) s = m[1];
    else s = s.replace(/[\/?#].*$/, '');
    s = s.replace(/^[^@]*@/, '').replace(/:\d*$/, '').replace(/^\.+|\.+$/g, '');
    if (!s) return { ok: false, value: '', error: '填一个网站，例如 oa.example.com。' };
    if (!DOMAIN_RE.test(s) || /\.\./.test(s)) return { ok: false, value: s, error: '「' + clip(s, 40) + '」不像网站域名。只能有字母、数字、点和短横线，例如 oa.example.com。' };
    return { ok: true, value: s, error: '' };
  }

  /** 往名单里加一个。返回 {list, value, error}；list 是新数组（出错时是原样的拷贝）。 */
  function addDomain(list, raw) {
    var cur = (Array.isArray(list) ? list : []).map(str);
    var d = normalizeDomain(raw);
    if (!d.ok) return { list: cur, value: d.value, error: d.error };
    if (cur.indexOf(d.value) >= 0) return { list: cur, value: d.value, error: '「' + d.value + '」已经在名单里了。' };
    return { list: cur.concat([d.value]), value: d.value, error: '' };
  }

  /* ---------------- 操作网页 / 桌面的步骤文字 ---------------- */

  /** 事件里的 display → {kind, title, page, host, text}；不是浏览器或桌面的、没有标题的返回 null。 */
  function displayOf(x) {
    if (!x || typeof x !== 'object' || Array.isArray(x)) return null;
    var kind = x.kind === 'desktop' ? 'desktop' : x.kind === 'browser' ? 'browser' : x.kind === 'kb' ? 'kb' : x.kind === 'tool_load' ? 'tool_load' : '';
    var title = clip(x.title, 160);
    if (!kind || !title) return null;
    return { kind: kind, title: title, page: clip(x.page, 120), host: clip(x.host, 120), text: str(x.text) };
  }

  /** 新的 display 盖在旧的上面（结果里的页面标题可能变了）；新的没有的字段沿用旧的。 */
  function mergeDisplay(old, next) {
    var n = displayOf(next);
    if (!n) return old || null;
    if (!old) return n;
    return { kind: n.kind, title: n.title || old.title, page: n.page || old.page, host: n.host || old.host, text: n.text || old.text };
  }

  /** 步骤行要显示的东西：有 display 时用它的标题和图标，没有时照旧用 toolInfo。
   * 返回 {verb, target, icon, where, hidden}；where 是"页面标题 · 域名"，展开后显示。 */
  function stepText(it) {
    it = it || {};
    var d = displayOf(it.display);
    if (d) {
      return { verb: d.title, target: '', icon: d.kind === 'desktop' ? 'monitor' : d.kind === 'kb' ? 'book' : d.kind === 'tool_load' ? 'tools' : 'globe',
               where: [d.page, d.host].filter(Boolean).join(' · '), hidden: false };
    }
    var info = toolInfo(it.tool, it.args, it.risk);
    return { verb: info.verb, target: info.target, icon: '', where: '', hidden: info.hidden };
  }

  /** 批准卡片上"以后都允许"那个按钮：browser_open → 这个网站；浏览器其他动作、桌面、删除、保存技能 → 不给。 */
  function alwaysOption(it) {
    it = it || {};
    var d = displayOf(it.display);
    if (it.risk === 'delete' || it.tool === 'skill_save') return null;
    if (it.risk === 'read_outside') return null;      // 想以后都直接读：改成「全放行」（卡片上有说明）
    if (it.tool === 'browser_open') {
      var host = d && d.host ? d.host : '';
      if (!host) { var m = /^https?:\/\/([^\/?#:]+)/i.exec(str(parseArgs(it.args).url)); host = m ? m[1].toLowerCase() : ''; }
      return { label: '以后都允许这个网站', title: host ? '以后打开 ' + host + ' 不再询问。可以在 设置 → 操作网页 里删掉' : '以后打开这个网站不再询问' };
    }
    if (/^(browser|desktop)_/.test(str(it.tool)) || it.risk === 'submit') return null;
    return { label: '以后都允许', title: '' };
  }

  function tokenFrom(search) {
    var m = /[?&]t=([^&#]*)/.exec(str(search));
    if (!m) return '';
    try { return decodeURIComponent(m[1]); } catch (e) { return m[1]; }
  }

  function apiUrl(path, params) {
    var q = [];
    Object.keys(params || {}).forEach(function (k) {
      var v = params[k];
      if (v === undefined || v === null || v === '') return;
      q.push(encodeURIComponent(k) + '=' + encodeURIComponent(v));
    });
    return path + (q.length ? (path.indexOf('?') >= 0 ? '&' : '?') + q.join('&') : '');
  }

  /* ---------------- 语音输入（纯函数） ---------------- */

  var VOICE_RATE = 16000;
  var SILENCE_PEAK = 0.008;   // 整段录音的峰值低于它（约 -42 dBFS）就当作没有声音

  /**
   * 重采样到 toRate。降采样时对每个输出点覆盖的输入区间取平均（相当于一个简单的低通，减少混叠），
   * 升采样时线性插值。输入、输出都是 [-1, 1] 的浮点样本。
   */
  function resample(input, fromRate, toRate) {
    var n = input ? input.length : 0;
    fromRate = +fromRate; toRate = +toRate;
    if (!n || !(fromRate > 0) || !(toRate > 0)) return new Float32Array(0);
    if (fromRate === toRate) return Float32Array.from ? Float32Array.from(input) : new Float32Array(input);
    var ratio = fromRate / toRate;
    var outLen = Math.max(1, Math.round(n / ratio));
    var out = new Float32Array(outLen);
    var i, k;
    if (ratio > 1) {
      for (i = 0; i < outLen; i++) {
        var a = i * ratio, b = Math.min(n, (i + 1) * ratio);
        var sum = 0, w = 0;
        var k0 = Math.floor(a);
        for (k = k0; k < b; k++) {
          var lo = Math.max(a, k), hi = Math.min(b, k + 1), part = hi - lo;
          if (part > 0) { sum += input[k] * part; w += part; }
        }
        out[i] = w > 0 ? sum / w : (k0 < n ? input[k0] : 0);
      }
    } else {
      for (i = 0; i < outLen; i++) {
        var x = i * ratio, j = Math.floor(x), f = x - j;
        var s0 = input[Math.min(j, n - 1)], s1 = input[Math.min(j + 1, n - 1)];
        out[i] = s0 + (s1 - s0) * f;
      }
    }
    return out;
  }

  /** 浮点样本 → 16 位 PCM 单声道 WAV（ArrayBuffer）。超出 [-1, 1] 的截断。 */
  function encodeWav(samples, sampleRate) {
    var n = samples ? samples.length : 0;
    var buf = new ArrayBuffer(44 + n * 2);
    var dv = new DataView(buf);
    function ascii(off, s) { for (var i = 0; i < s.length; i++) dv.setUint8(off + i, s.charCodeAt(i)); }
    ascii(0, 'RIFF');
    dv.setUint32(4, 36 + n * 2, true);
    ascii(8, 'WAVE');
    ascii(12, 'fmt ');
    dv.setUint32(16, 16, true);            // fmt 块长度
    dv.setUint16(20, 1, true);             // PCM
    dv.setUint16(22, 1, true);             // 单声道
    dv.setUint32(24, sampleRate, true);
    dv.setUint32(28, sampleRate * 2, true); // 每秒字节数
    dv.setUint16(32, 2, true);             // 每帧字节数
    dv.setUint16(34, 16, true);            // 位深
    ascii(36, 'data');
    dv.setUint32(40, n * 2, true);
    for (var i = 0; i < n; i++) {
      var s = samples[i];
      if (!(s === s)) s = 0;               // NaN
      s = s > 1 ? 1 : s < -1 ? -1 : s;
      dv.setInt16(44 + i * 2, s < 0 ? Math.round(s * 0x8000) : Math.round(s * 0x7fff), true);
    }
    return buf;
  }

  /** 把多段浮点样本接成一段，最多 maxLen 个。 */
  function concatSamples(chunks, maxLen) {
    var total = 0, i;
    for (i = 0; i < chunks.length; i++) total += chunks[i].length;
    if (maxLen >= 0 && total > maxLen) total = maxLen;
    var out = new Float32Array(total), off = 0;
    for (i = 0; i < chunks.length && off < total; i++) {
      var c = chunks[i];
      var take = Math.min(c.length, total - off);
      out.set(take === c.length ? c : c.subarray(0, take), off);
      off += take;
    }
    return out;
  }

  /**
   * 把文字插入到输入框的光标处（有选区时替换选区）。返回 {value, caret}。
   * 英文、数字和前后的英文、数字挨着时补一个空格；中文不加空格。
   */
  function insertAtCursor(value, start, end, text) {
    value = str(value); text = str(text);
    var len = value.length;
    function pos(x) { x = Number(x); return x === x && x >= 0 ? Math.min(Math.floor(x), len) : len; }
    var s = (start === null || start === undefined) ? len : pos(start);
    var e = (end === null || end === undefined) ? s : pos(end);
    if (e < s) { var t = s; s = e; e = t; }
    var before = value.slice(0, s), after = value.slice(e);
    var ins = text;
    if (ins && /[A-Za-z0-9]$/.test(before) && /^[A-Za-z0-9]/.test(ins)) ins = ' ' + ins;
    if (ins && /[A-Za-z0-9.,!?]$/.test(ins) && /^[A-Za-z0-9]/.test(after)) ins = ins + ' ';
    return { value: before + ins + after, caret: before.length + ins.length };
  }

  /** 录音时长：5 → "0:05"，83 → "1:23"。 */
  function formatDuration(sec) {
    sec = Math.max(0, Math.floor(Number(sec) || 0));
    var m = Math.floor(sec / 60), s = sec % 60;
    return m + ':' + (s < 10 ? '0' : '') + s;
  }

  /** 音量（均方根）→ 0..1 的指示条长度：-60 dB 以下为 0，-10 dB 以上为 1。 */
  function meterLevel(rms) {
    rms = Number(rms) || 0;
    if (rms <= 0) return 0;
    var db = 20 * Math.log(rms) / Math.LN10;
    return Math.max(0, Math.min(1, (db + 60) / 50));
  }

  /** 浏览器的麦克风错误名 → 错误种类。 */
  function micErrorKind(name) {
    switch (str(name)) {
      case 'NotAllowedError': case 'PermissionDeniedError': case 'SecurityError': return 'denied';
      case 'NotFoundError': case 'DevicesNotFoundError': case 'OverconstrainedError': return 'no-device';
      case 'NotReadableError': case 'TrackStartError': case 'AbortError': return 'busy';
      default: return 'mic-failed';
    }
  }

  /** 识别接口的 HTTP 状态码 → 错误种类。0 表示请求没有到达 Agent 程序。 */
  function httpErrorKind(status) {
    status = Number(status) || 0;
    if (status === 409) return 'not-configured';
    if (status === 400 || status === 413 || status === 415) return 'bad-audio';
    if (status === 502 || status === 503 || status === 504) return 'unavailable';
    if (status === 403) return 'forbidden';
    if (status === 0) return 'offline';
    return 'failed';
  }

  var VOICE_ERRORS = {
    'unsupported': '这个浏览器不能录音。请用最新版的 Chrome、Edge 或 Safari 打开。',
    'denied': '没有麦克风权限。请点浏览器地址栏旁边的麦克风或锁形图标，允许使用麦克风，再点一次麦克风按钮。如果已经允许还不行，到系统设置的“隐私”里允许浏览器使用麦克风。',
    'no-device': '没有找到麦克风。请接上麦克风或耳机，再试一次。',
    'busy': '麦克风被别的程序占用了，或者暂时打不开。关掉正在用麦克风的会议软件后再试。',
    'mic-failed': '打不开麦克风，请再试一次。',
    'silent': '没有听到声音。请检查麦克风是否静音、是否选对了输入设备，然后再试一次。',
    'not-configured': '语音识别还没有设置好。',
    'unavailable': '识别服务暂时连不上。请稍后再试；一直不行请联系管理员。',
    'timeout': '识别太久没有结果，已经放弃。请稍后再试，或者说短一点。',
    'empty': '没有识别出文字。请离麦克风近一点、说慢一点，再试一次。',
    'bad-audio': '这段录音没法识别。',
    'forbidden': '没有权限。请用启动时打印的地址重新打开。',
    'offline': '连不上 Agent 程序。它可能已经关闭，请重新运行启动脚本。',
    'failed': '识别没有成功，请再试一次。'
  };

  /**
   * 错误种类 → 给用户看的提示。detail 是服务端给的中文说明，在“没设置好”“录音不合法”时更具体，优先用它。
   * 返回 {text, settings}；settings 为 true 时提示里带“去设置”按钮。
   */
  function voiceError(kind, detail) {
    var base = VOICE_ERRORS[kind] || VOICE_ERRORS.failed;
    var d = str(detail).trim();
    var text = base;
    if (d && (kind === 'not-configured' || kind === 'bad-audio')) text = d;
    return { text: text, settings: kind === 'not-configured' };
  }

  /** Ctrl+Shift+M 或 ⌘+Shift+M。 */
  function isVoiceShortcut(e) {
    if (!e || !e.shiftKey || e.altKey || !(e.ctrlKey || e.metaKey)) return false;
    return e.code === 'KeyM' || e.key === 'm' || e.key === 'M';
  }

  /** 内核名 → 显示文字。 */
  function kernelLabel(name) {
    if (name === 'langgraph') return 'LangChain / LangGraph';
    if (name === 'builtin' || !name) return '过渡内核';
    return str(name);
  }

  /* ---------------- 记忆、搜索、技能（纯函数） ---------------- */

  var ORIGIN_LABEL = { user: '你添加的', chat: '对话中记下', review: '自动整理' };
  var TARGET_LABEL = { user: '关于我', workspace: '这个文件夹' };
  var MISSING_API = '这个功能需要新版 Agent 程序。请关掉 Agent 程序，重新运行启动脚本后再试。';

  /** 记忆的来源 → 给人看的说法。 */
  function originLabel(origin) { return ORIGIN_LABEL[origin] || ORIGIN_LABEL.user; }
  function targetLabel(t) { return TARGET_LABEL[t] || TARGET_LABEL.user; }

  /** 任务时间线里一条记忆提示的文字。item 或事件 payload 都可以：{op, origin, text}。 */
  function memoryNoteText(p) {
    p = p || {};
    var verb = p.op === 'replace' ? '更新了记忆：' : p.op === 'remove' ? '删掉了一条记忆：' : '记住了：';
    return (p.origin === 'review' ? '自动整理 · ' : '') + verb + str(p.text);
  }

  /** 用量 → "已用 41%"；没有用量时返回空串（界面就不显示）。 */
  function usageText(u) {
    if (!u || typeof u !== 'object') return '';
    var pct = Number(u.pct);
    if (!(pct === pct) && Number(u.budget) > 0 && Number(u.used) >= 0) pct = Math.round(Number(u.used) * 100 / Number(u.budget));
    if (!(pct === pct) || pct < 0) return '';
    pct = Math.round(pct);
    return pct >= 100 ? '已满' : '已用 ' + pct + '%';
  }

  /** 用量条的宽度（0–100）。 */
  function usagePct(u) {
    if (!u || typeof u !== 'object') return 0;
    var pct = Number(u.pct);
    if (!(pct === pct) && Number(u.budget) > 0) pct = Number(u.used) * 100 / Number(u.budget);
    return pct === pct ? Math.max(0, Math.min(100, Math.round(pct))) : 0;
  }

  /** 日期：秒数或 "2026-11-18" 都行。今年的不写年份。 */
  function shortDate(x, nowMs) {
    var d = null;
    if (typeof x === 'number' && x > 0) d = new Date(x * 1000);
    else if (typeof x === 'string') {
      var m = /^(\d{4})-(\d{1,2})-(\d{1,2})/.exec(x.trim());
      if (m) d = new Date(+m[1], +m[2] - 1, +m[3]);
      else return x.trim();
    }
    if (!d || isNaN(d.getTime())) return '';
    var now = new Date(nowMs || Date.now());
    if (d.toDateString() === now.toDateString()) return '今天';
    if (d.toDateString() === new Date(now.getTime() - 86400000).toDateString()) return '昨天';
    var md = (d.getMonth() + 1) + '月' + d.getDate() + '日';
    return d.getFullYear() === now.getFullYear() ? md : d.getFullYear() + '年' + md;
  }

  function normEntry(m, target) {
    var origin = m.origin;
    if (!ORIGIN_LABEL[origin]) origin = /^s_/.test(str(m.source)) ? 'chat' : 'user';
    var sid = m.session_id || (origin === 'chat' && /^s_/.test(str(m.source)) ? m.source : null);
    return { id: str(m.id), target: target, text: str(m.text), origin: origin, session_id: sid ? str(sid) : null,
             created: Number(m.created) || 0, updated: Number(m.updated) || Number(m.created) || 0 };
  }

  function normUsage(u) {
    return u && typeof u === 'object' && (u.pct != null || u.budget != null) ? u : null;
  }

  /**
   * GET /api/memories 的返回 → {user, workspace, usage:{user, workspace}, settings, legacy}。
   * 兼容旧格式：直接是一个列表，条目没有 target，按 workspace_id 有没有值来分。
   */
  function normalizeMemories(d, wsId) {
    var legacy = Array.isArray(d);
    var list = legacy ? d : (d && Array.isArray(d.memories) ? d.memories : []);
    var out = { user: [], workspace: [], usage: { user: null, workspace: null }, settings: null, legacy: legacy };
    list.forEach(function (m) {
      if (!m || typeof m !== 'object' || m.id == null) return;
      var t = m.target === 'user' || m.target === 'workspace' ? m.target : (m.workspace_id ? 'workspace' : 'user');
      if (t === 'workspace' && m.workspace_id && wsId && m.workspace_id !== wsId) return;
      out[t].push(normEntry(m, t));
    });
    out.user.sort(function (a, b) { return a.created - b.created; });
    out.workspace.sort(function (a, b) { return a.created - b.created; });
    if (!legacy && d && d.usage && typeof d.usage === 'object') {
      out.usage.user = normUsage(d.usage.user);
      out.usage.workspace = normUsage(d.usage.workspace);
    }
    if (!legacy && d && d.settings && typeof d.settings === 'object') {
      out.settings = { enabled: d.settings.enabled !== false, auto: d.settings.auto !== false };
    }
    return out;
  }

  var OP_LABEL = { add: '添加', replace: '修改', remove: '删除' };

  /** "最近的改动"里的一行。 */
  function describeLog(r) {
    r = r || {};
    var op = OP_LABEL[r.op] ? r.op : 'add';
    return {
      id: r.id, op: op, opLabel: OP_LABEL[op], target: targetLabel(r.target || (r.workspace_id ? 'workspace' : 'user')),
      text: str(op === 'remove' ? (r.before || r.after) : (r.after || r.before)),
      before: op === 'replace' ? str(r.before) : '',
      origin: originLabel(r.origin), ts: Number(r.ts) || 0, undone: !!r.undone, session_id: r.session_id || null
    };
  }

  function normalizeLog(d) {
    var list = Array.isArray(d) ? d : (d && Array.isArray(d.log) ? d.log : []);
    return list.filter(function (r) { return r && typeof r === 'object' && r.id != null; }).map(describeLog);
  }

  /** GET /api/skills → [{name, description, source}]；没有 source 的当内置。"我的"排在前面。 */
  function normalizeSkills(d) {
    var list = Array.isArray(d) ? d : (d && Array.isArray(d.skills) ? d.skills : []);
    var out = [];
    list.forEach(function (s) {
      if (!s || typeof s !== 'object' || !s.name) return;
      // 第 5.4 节：公司共享目录里的技能 source 是 shared
      out.push({ name: str(s.name), description: str(s.description), source: s.source === 'user' ? 'user' : s.source === 'shared' ? 'shared' : 'builtin' });
    });
    var rank = { user: 0, shared: 1, builtin: 2 };
    return out.map(function (x, i) { return { x: x, i: i }; }).sort(function (a, b) { return rank[a.x.source] - rank[b.x.source] || a.i - b.i; }).map(function (o) { return o.x; });
  }

  /** 搜索框里的文字 → 查询词（去掉首尾空白，内部空白压成一个）。 */
  function searchQuery(raw) { return str(raw).replace(/\s+/g, ' ').trim().slice(0, 200); }

  var ROLE_LABEL = { user: '你', '用户': '你', assistant: 'Agent', '助手': 'Agent' };

  /** GET /api/search → {hits:[{session_id, title, date, matches:[{seq, role, snippet}]}], note}。 */
  function normalizeHits(d) {
    var list = Array.isArray(d) ? d : (d && Array.isArray(d.hits) ? d.hits : []);
    var hits = [], seen = {};
    list.forEach(function (x) {
      if (!x || typeof x !== 'object' || !x.session_id || seen[x.session_id]) return;
      seen[x.session_id] = true;
      var ms = (Array.isArray(x.matches) ? x.matches : []).filter(function (m) { return m && typeof m === 'object' && m.snippet; })
        .map(function (m) {
          return { seq: m.seq, role: ROLE_LABEL[m.role] || '', snippet: str(m.snippet).replace(/\s+/g, ' ').trim() };
        });
      hits.push({ session_id: str(x.session_id), title: str(x.title) || '未命名对话', date: x.date || x.updated || '', matches: ms });
    });
    return { hits: hits, note: d && !Array.isArray(d) && d.note ? str(d.note) : '' };
  }

  /** 侧栏搜索结果上方的那一句话。st：{q, loading, error, hits}。 */
  function searchStatusText(st) {
    st = st || {};
    var n = Array.isArray(st.hits) ? st.hits.length : 0;
    if (st.loading && !n) return '正在查找…';
    if (st.error) return str(st.error);
    if (!n) return '没有找到包含“' + clip(st.q, 20) + '”的对话。换个说法试试。';
    return '找到 ' + n + ' 个对话' + (st.loading ? '，正在更新…' : '');
  }

  /** 把文字按查询词切成 [{t, hit}]，界面把 hit 的部分标出来。不区分大小写。 */
  function highlightParts(text, query) {
    text = str(text);
    var terms = searchQuery(query).toLowerCase().split(' ').filter(Boolean).slice(0, 8);
    if (!text || !terms.length) return text ? [{ t: text, hit: false }] : [];
    var low = text.toLowerCase(), ranges = [];
    terms.forEach(function (w) {
      var i = 0;
      while ((i = low.indexOf(w, i)) >= 0 && ranges.length < 200) { ranges.push([i, i + w.length]); i += w.length; }
    });
    if (!ranges.length) return [{ t: text, hit: false }];
    ranges.sort(function (a, b) { return a[0] - b[0] || b[1] - a[1]; });
    var merged = [];
    ranges.forEach(function (r) {
      var last = merged[merged.length - 1];
      if (last && r[0] <= last[1]) last[1] = Math.max(last[1], r[1]);
      else merged.push([r[0], r[1]]);
    });
    var parts = [], pos = 0;
    merged.forEach(function (r) {
      if (r[0] > pos) parts.push({ t: text.slice(pos, r[0]), hit: false });
      parts.push({ t: text.slice(r[0], r[1]), hit: true });
      pos = r[1];
    });
    if (pos < text.length) parts.push({ t: text.slice(pos), hit: false });
    return parts;
  }

  /** 接口出错 → 给人看的一句话。老版本程序没有新接口时（404 没有这个接口、405）给统一的说明。 */
  function apiErrorMessage(status, data) {
    var err = data && typeof data === 'object' && data.error ? str(data.error) : '';
    if (status === 405 || (status === 404 && err === '没有这个接口')) return MISSING_API;
    if (err) return err;
    if (status === 403) return '没有权限。请用启动时打印的地址重新打开。';
    if (status === 404) return '找不到要的东西，可能已经被删除。';
    return '操作没有成功（' + status + '）。';
  }

  /** 从 Content-Disposition 里取文件名（支持 filename*=UTF-8''…）。取不到时用 fallback。 */
  function dispositionName(header, fallback) {
    var h = str(header), name = '';
    var m = /filename\*\s*=\s*(?:UTF-8|utf-8)?''([^;]+)/.exec(h);
    if (m) { try { name = decodeURIComponent(m[1].trim().replace(/^"|"$/g, '')); } catch (e) { name = ''; } }
    if (!name) { m = /filename\s*=\s*"([^"]*)"|filename\s*=\s*([^;]+)/.exec(h); if (m) name = (m[1] != null ? m[1] : m[2]).trim(); }
    name = name.replace(/[\\\/:*?"<>|\u0000-\u001f]+/g, '_').trim();
    return name || str(fallback) || '对话记录.md';
  }

  /** 导出对话记录的地址。和 /api/file 的下载链接一样，令牌放在查询参数 t 里。 */
  function exportUrl(sid, token) {
    return apiUrl('/api/sessions/' + encodeURIComponent(str(sid)) + '/export', { t: token });
  }

  /* ---------------- 聊天界面：步骤折叠、附件、文件夹（纯函数） ---------------- */

  var STEP_ERR = { failed: true, unknown: true };
  var STEP_LIVE = { running: true, planned: true, approved: true };

  /** 一个工具条目算不算"一步"：等批准的单独显示成卡片；计划、提问这类不显示的工具不算（出错时例外）。 */
  function isStep(it) {
    if (!it || it.kind !== 'tool' || it.state === 'awaiting') return false;
    return !toolInfo(it.tool, it.args, it.risk).hidden || it.state === 'failed';
  }

  /**
   * 一轮里的步骤 → 折叠后的那一行。
   * opts.live：这一轮是最后一轮，而且对话还在运行；opts.waiting：'approval' 或 'question'，这一轮停在等你确认。
   * 返回 {text, note, running, failed, count, waiting, current}。
   */
  function foldSteps(steps, opts) {
    opts = opts || {};
    var list = (steps || []).filter(isStep);
    var n = list.length, failed = 0, cur = null, looped = 0;
    list.forEach(function (s) {
      // 被拦下的重复调用（第 3.8 节）不算出错，单独说
      if (s.looped) looped++;
      else if (STEP_ERR[s.state]) failed++;
      if (STEP_LIVE[s.state]) cur = s;
    });
    var running = !!opts.live && n > 0;
    var waiting = opts.waiting === 'approval' || opts.waiting === 'question' ? opts.waiting : '';
    var text, note = '';
    if (running) {
      var show = cur || list[n - 1];
      var info = toolInfo(show.tool, show.args, show.risk);
      text = cur ? '正在：' + info.verb + (info.target ? ' ' + info.target : '') + '…' : '做了 ' + n + ' 步，还在继续…';
      if (failed) note = '有 ' + failed + ' 步出错';
    } else {
      text = '做了 ' + n + ' 步' + (failed ? '，其中 ' + failed + ' 步出错' : '') + (looped ? '，拦下 ' + looped + ' 次重复的调用' : '');
      if (waiting) text += waiting === 'approval' ? '，等你批准' : '，等你回答';
    }
    // 用了多久：第一步开始到最后一步结束（还在做时不算）
    var t0 = 0, t1 = 0;
    list.forEach(function (s) {
      if (s.startedAt && (!t0 || s.startedAt < t0)) t0 = s.startedAt;
      if (s.endedAt && s.endedAt > t1) t1 = s.endedAt;
    });
    var secs = !running && t0 && t1 >= t0 ? t1 - t0 : null;
    return { text: text, note: note, running: running, failed: failed, looped: looped, count: n, waiting: waiting,
             current: running && cur ? cur.key : null, secs: secs };
  }

  /**
   * 时间线条目 → 显示用的列表。用户的一句话开始新的一轮；一轮里的步骤收成一行（放在第一步的位置），
   * 等批准、提问卡片照常单独显示，不折进去；这一轮做出来的文件收成文件卡片，放在这一轮最后。
   * 返回 [{type:'item', key, item} | {type:'steps', key, steps, fold} | {type:'files', key, files}]。
   */
  function groupTimeline(items, status) {
    items = Array.isArray(items) ? items : [];
    var out = [], turn = null;
    function begin(key) { turn = { key: key, group: null, files: [], waiting: '' }; }
    function close(last) {
      if (!turn) return;
      if (turn.group) {
        turn.group.fold = foldSteps(turn.group.steps, { live: !!last && status === 'running', waiting: turn.waiting });
      }
      if (turn.files.length) {
        var byPath = {}, order = [];
        turn.files.forEach(function (f) {
          var cur = byPath[f.path];
          if (!cur) { order.push(f.path); byPath[f.path] = f; }
          else if ((f.version || 1) >= (cur.version || 1)) byPath[f.path] = f;
        });
        out.push({ type: 'files', key: 'f' + turn.key, files: order.map(function (p) { return byPath[p]; }) });
      }
    }
    items.forEach(function (it) {
      if (!it) return;
      if (it.kind === 'user') {
        close(false);
        begin(it.key);
        out.push({ type: 'item', key: it.key, item: it });
        return;
      }
      if (!turn) begin('start');
      if (isStep(it)) {
        if (!turn.group) { turn.group = { type: 'steps', key: 'g' + turn.key, steps: [], fold: null }; out.push(turn.group); }
        turn.group.steps.push(it);
        return;
      }
      if (it.kind === 'tool' && it.state !== 'awaiting') return;     // 不显示的工具（计划、提问）
      if (it.kind === 'tool') turn.waiting = 'approval';
      if (it.kind === 'question' && !it.answered && status === 'waiting_input' && !turn.waiting) turn.waiting = 'question';
      if (it.kind === 'artifact') { if (it.path) turn.files.push(it); return; }
      out.push({ type: 'item', key: it.key, item: it });
    });
    close(true);
    return out;
  }

  /** 附件列表的归一化：接受路径字符串或 {path, name, size}，去重，去掉空的。 */
  function normalizeFiles(list) {
    var out = [], seen = {};
    (Array.isArray(list) ? list : []).forEach(function (f) {
      var obj = f && typeof f === 'object' ? f : null;
      var p = typeof f === 'string' ? f : obj ? str(obj.path) : '';
      p = p.replace(/\\/g, '/').trim();
      if (!p || seen[p]) return;
      seen[p] = true;
      var size = obj && obj.size != null && obj.size !== '' && !isNaN(obj.size) ? Number(obj.size) : null;
      out.push({ path: p, name: (obj && obj.name ? str(obj.name) : '') || fileName(p), size: size });
    });
    return out;
  }

  /** 输入框上方的附件（上传中、传好了、失败）→ 能发出去的路径和还没好的个数。 */
  function attachState(list) {
    var r = { paths: [], uploading: 0, failed: 0 };
    (Array.isArray(list) ? list : []).forEach(function (a) {
      if (!a) return;
      if (a.state === 'done' && a.path) { if (r.paths.indexOf(a.path) < 0) r.paths.push(a.path); }
      else if (a.state === 'error') r.failed++;
      else r.uploading++;
    });
    return r;
  }

  /** 发送按钮现在是"发送"还是"停止"：输入框里有东西就是发送（运行中可以随时补充要求）；空着且正在做事时是停止。 */
  function sendMode(status, hasContent) {
    if (hasContent) return 'send';
    return status === 'running' || status === 'waiting_approval' ? 'stop' : 'send';
  }

  /** 默认空间的编号。旧版后端没有这个字段时返回 null，界面退回"先选文件夹"的流程。 */
  function defaultWorkspaceId(info, workspaces) {
    var list = Array.isArray(workspaces) ? workspaces : [];
    var id = info && info.default_workspace_id;
    if (id != null && id !== '' && list.some(function (w) { return w && w.id === id; })) return id;
    for (var i = 0; i < list.length; i++) if (list[i] && list[i].is_default) return list[i].id;
    return null;
  }

  /** 打开时落在哪个空间：有默认空间就用它；旧版后端用上次用过的，没有就用第一个。 */
  function startWorkspace(info, workspaces, saved) {
    var list = Array.isArray(workspaces) ? workspaces : [];
    var def = defaultWorkspaceId(info, list);
    if (def != null) return def;
    if (saved && list.some(function (w) { return w && w.id === saved; })) return saved;
    return list[0] ? list[0].id : null;
  }

  /** 侧栏"文件夹"小节：不含默认空间，按名字排，标出当前的。 */
  function folderList(workspaces, defaultId, currentId) {
    var list = (Array.isArray(workspaces) ? workspaces : []).filter(function (w) {
      return w && w.id != null && w.id !== defaultId && !(defaultId == null ? false : w.is_default);
    });
    list = list.map(function (w) {
      return { id: w.id, name: str(w.name) || fileName(w.path) || '文件夹', path: str(w.path), current: w.id === currentId };
    });
    list.sort(function (a, b) {
      var c = a.name.localeCompare(b.name, 'zh-Hans-CN');
      return c || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
    });
    return list;
  }

  /** 新对话页下面那一行建议：每个岗位轮流取一条，最多 n 条。 */
  function pickSuggestions(groups, n) {
    var out = [];
    n = n || 4;
    groups = Array.isArray(groups) ? groups : [];
    for (var round = 0; out.length < n; round++) {
      var any = false;
      for (var g = 0; g < groups.length && out.length < n; g++) {
        var it = groups[g] && groups[g].items && groups[g].items[round];
        if (it) { out.push(it); any = true; }
      }
      if (!any) break;
    }
    return out;
  }

  /** 拖进窗口的是不是文件（拖一段文字、一个链接时不提示）。 */
  function isFileDrag(dt) {
    if (!dt || !dt.types) return false;
    var t = dt.types;
    for (var i = 0; i < t.length; i++) if (t[i] === 'Files') return true;
    return typeof t.contains === 'function' ? !!t.contains('Files') : false;
  }

  var UPLOAD_MISSING = '这个版本的 Agent 程序还不能直接收文件。请更新程序，或者把文件放进一个文件夹，再用这个文件夹工作。';

  /** 上传出错 → 一句话。 */
  function uploadErrorText(status, data) {
    var err = data && typeof data === 'object' && data.error ? str(data.error) : '';
    if (status === 405 || (status === 404 && (!err || err === '没有这个接口'))) return UPLOAD_MISSING;
    if (status === 413) return err || '文件太大了，传不上去。';
    if (status === 404 && err === '没有这个工作区') return '这个文件夹已经不在列表里了，文件没有传上去。';
    return apiErrorMessage(status, data);
  }

  function uploadUrl(wsId, name) {
    return apiUrl('/api/upload', { workspace: wsId, name: name });
  }

  var IMAGE_TYPE = /^image\/(png|jpe?g|gif|webp|bmp)$/i;

  /** 拖进来、粘贴进来、选出来的文件分两类：能给模型看的图片走原来的看图路径，其他的上传。 */
  function splitFiles(list) {
    var r = { images: [], others: [] };
    Array.prototype.forEach.call(list || [], function (f) {
      if (!f) return;
      if (IMAGE_TYPE.test(str(f.type))) r.images.push(f); else r.others.push(f);
    });
    return r;
  }

  /** 上传上限（字节）。后端在 /api/state 里给了 upload_max_mb 就用它，否则按 50 MB。 */
  function uploadLimit(info) {
    var mb = Number(info && info.upload_max_mb);
    return (mb > 0 ? mb : 50) * 1024 * 1024;
  }

  /** 文件超过上传上限时的那句话。 */
  function tooBigText(name, limit) {
    var mb = Math.round(Number(limit) / 1048576 * 10) / 10;
    return '「' + (str(name) || '这个文件') + '」太大了，最多 ' + mb + ' MB。';
  }

  /** 比较两个文件夹路径：统一斜杠，去掉末尾的斜杠；Windows 盘符路径不分大小写。 */
  function samePath(a, b) {
    function norm(p) {
      p = str(p).trim().replace(/\\/g, '/');
      if (p.length > 1) p = p.replace(/\/+$/, '') || '/';
      return /^[a-z]:\//i.test(p) ? p.toLowerCase() : p;
    }
    var x = norm(a);
    return !!x && x === norm(b);
  }

  /** 要不要显示整屏的"先选一个文件夹"：只在旧版后端（没有默认空间）而且一个文件夹都没有时。 */
  function needsOnboard(workspaces, defaultId) {
    var n = Array.isArray(workspaces) ? workspaces.length : 0;
    return defaultId == null && n === 0;
  }

  /** 折叠的那一行的签名：内容不变就不重画（流式输出时每帧都会走到这里）。 */
  function stepsSig(entry, openMap) {
    openMap = openMap || {};
    var f = entry && entry.fold ? entry.fold : {};
    var parts = [f.text, f.note, f.running ? 1 : 0, f.failed, openMap[entry.key] ? 1 : 0];
    (entry && entry.steps || []).forEach(function (s) { parts.push(s.key + ':' + (s.v || 0) + ':' + s.state + ':' + (openMap[s.key] ? 1 : 0)); });
    return parts.join('|');
  }

  /** 文件卡片的签名。 */
  function filesSig(files) {
    return (files || []).map(function (f) { return f.path + '@' + (f.version || 1); }).join('|');
  }

  /* ---------------- 两种工作模式：CariAi 和 Hspace（docs/两种模式.md） ----------------
   * 两个模式的文字全部来自 /api/state 的 modes，界面不写死。老后端没有 modes：整理结果是空数组，界面一切照旧。
   */

  var MODE_IDS = { cariai: true, hspace: true };

  /** /api/state → [{id, name, tagline, prompt_line, placeholder}]。认不出、少于两个时返回 []（不显示总开关）。 */
  function normalizeModes(info) {
    var list = info && typeof info === 'object' && Array.isArray(info.modes) ? info.modes : [];
    var out = [], seen = {};
    list.forEach(function (m) {
      if (!m || typeof m !== 'object') return;
      var id = str(m.id).trim().toLowerCase();
      if (!/^[a-z][a-z0-9_-]{0,31}$/.test(id) || seen[id]) return;
      seen[id] = true;
      out.push({ id: id, name: clip(m.name, 24) || id, tagline: clip(m.tagline, 80), prompt_line: clip(m.prompt_line, 40),
                 placeholder: clip(m.placeholder, 80) });
    });
    return out.length >= 2 ? out : [];
  }

  function findModeById(modes, id) {
    for (var i = 0; i < (modes || []).length; i++) if (modes[i].id === id) return modes[i];
    return null;
  }

  /** 该用哪个模式：先看 want（地址里写的），再看 saved（上次记住的），都不行用第一个。没有模式（老后端）时是 ''。 */
  function pickMode(modes, want, saved) {
    if (!modes || !modes.length) return '';
    if (want && findModeById(modes, want)) return want;
    if (saved && findModeById(modes, saved)) return saved;
    return modes[0].id;
  }

  /** 一个对话属于哪个模式。老记录没有 mode，算 CariAi。 */
  function sessionMode(s) {
    var m = s && typeof s === 'object' ? str(s.mode).toLowerCase() : '';
    return m || 'cariai';
  }

  /** 侧栏只列当前模式的对话。mode 为空（老后端）时原样返回。 */
  function sessionsForMode(list, mode) {
    list = Array.isArray(list) ? list : [];
    if (!mode) return list;
    return list.filter(function (s) { return sessionMode(s) === mode; });
  }

  /** 地址 → {mode, path}。#/hspace、#/hspace/task/x、#/cariai/kb；不带模式的（#/new、#/task/x）mode 为 ''。path 为空时是 'new'。 */
  function parseHash(hash) {
    var hs = str(hash).replace(/^#\/?/, '').replace(/\/+$/, '');
    var parts = hs ? hs.split('/') : [];
    var mode = '';
    if (parts.length && MODE_IDS[parts[0].toLowerCase()]) mode = parts.shift().toLowerCase();
    var path = parts.join('/');
    return { mode: mode, path: path || 'new' };
  }

  /** {mode, path} → 地址。CariAi 和老后端不带前缀（以前的地址都还能用）；Hspace 带 #/hspace。 */
  function buildHash(mode, path) {
    path = str(path).replace(/^#?\/*/, '') || 'new';
    if (mode === 'hspace') return '#/hspace' + (path === 'new' ? '' : '/' + path);
    return '#/' + path;
  }

  /** 界面里各处写的 go('#/new')、go('#/kb')：没写模式时补上当前模式。对话的地址按对话自己的模式（knownMode）补。 */
  function withMode(hash, mode, knownMode) {
    var r = parseHash(hash);
    if (r.mode) return buildHash(r.mode, r.path);
    if (/^task\//.test(r.path)) return buildHash(knownMode || '', r.path);
    return buildHash(mode, r.path);
  }

  /** 侧栏导航有哪几项（规格 9.1：两个模式一样，Hspace 里"知识库"排第一，CariAi 里排在后面）。老后端照旧。 */
  function navItems(mode) {
    if (mode === 'hspace') return ['kb', 'memory', 'schedule', 'skills', 'settings'];
    if (mode) return ['memory', 'schedule', 'skills', 'kb', 'settings'];
    return ['kb', 'memory', 'schedule', 'skills', 'settings'];
  }

  /* ---------------- 出处编号 ---------------- */

  var CITE_RE = /\[(\d{1,4}(?:\s*[,，、]\s*\d{1,4})*)\](?!\()/g;

  function citeNums(inner) {
    return str(inner).split(/\s*[,，、]\s*/).map(function (x) { return parseInt(x, 10); }).filter(function (n) { return n > 0; });
  }

  /** 一段文字 → [{text} | {cite: [n…], raw}]。只认 has(n) 为真的编号；括号里有一个不认识就整段当普通文字。 */
  function splitCites(text, has) {
    var s = str(text), out = [], last = 0, m;
    CITE_RE.lastIndex = 0;
    while ((m = CITE_RE.exec(s))) {
      var nums = citeNums(m[1]);
      if (!nums.length || !nums.every(function (n) { return has(n); })) continue;
      if (m.index > last) out.push({ text: s.slice(last, m.index) });
      out.push({ cite: nums, raw: m[0] });
      last = m.index + m[0].length;
    }
    if (last < s.length) out.push({ text: s.slice(last) });
    return out;
  }

  /** 去掉代码块和行内代码（里面的 [1] 不算出处）。 */
  function stripCode(md) {
    return str(md).replace(/(^|\n)(```|~~~)[^\n]*\n[\s\S]*?(\n\2[^\n]*(?=\n|$)|$)/g, '$1').replace(/`[^`\n]+`/g, '');
  }

  /** 一段回答里引用了哪些编号（去重，按出现顺序）。只算 has(n) 为真的；代码里的不算。 */
  function citedNums(text, has) {
    var seen = {}, out = [];
    splitCites(stripCode(stripThink(text)), has).forEach(function (p) {
      (p.cite || []).forEach(function (n) { if (!seen[n]) { seen[n] = true; out.push(n); } });
    });
    return out;
  }

  /** 回答下面那一行小字。Hspace 一处没引用时说明；CariAi 没引用时不写。 */
  function basisText(count, mode) {
    if (count > 0) return '依据 ' + count + ' 处资料';
    return mode === 'hspace' ? '这段回答没有引用资料' : '';
  }

  var VIA_TEXT = { keyword: '关键词', vector: '语义', both: '关键词 + 语义' };

  /** via → 'keyword' | 'vector' | 'both' | ''。接受字符串（keyword / vector / both / hybrid / bm25 / semantic）或数组。 */
  function normVia(v) {
    var list = Array.isArray(v) ? v.map(str) : str(v).toLowerCase().split(/[\s,+|]+/);
    var k = false, vec = false;
    list.forEach(function (x) {
      x = str(x).toLowerCase();
      if (x === 'both' || x === 'hybrid') { k = vec = true; }
      else if (x === 'keyword' || x === 'bm25' || x === 'fts' || x === 'lexical') k = true;
      else if (x === 'vector' || x === 'semantic' || x === 'embedding' || x === 'dense') vec = true;
    });
    return k && vec ? 'both' : k ? 'keyword' : vec ? 'vector' : '';
  }
  function viaLabel(v) { return VIA_TEXT[normVia(v)] || ''; }

  /** 一条命中（规格 3.1 的 hits）→ 界面用的形状。没有编号也收（检索测试的结果没有编号），n 为 0。 */
  function normKbHit(x) {
    if (!x || typeof x !== 'object') return null;
    var n = parseInt(x.n, 10);
    var title = str(x.title) || fileName(str(x.path)) || '资料';
    return { n: n > 0 ? n : 0, doc_id: str(x.doc_id), part_id: str(x.part_id), library: str(x.library), title: title,
             path: str(x.path), loc: str(x.loc), heading: str(x.heading), text: str(x.text), via: normVia(x.via),
             score: typeof x.score === 'number' && isFinite(x.score) ? x.score : null,
             // 规格 9.2 / 9.5：问答对、版本、旧版、标准答案
             kind: str(x.kind), version_label: str(x.version_label), superseded: x.superseded === true,
             newer: str(x.newer), standard: x.standard === true,
             // 第 4.2 节：标准答案用过几次、和问题几乎一样
             uses: typeof x.hits === 'number' && x.hits >= 0 ? Math.round(x.hits) : null, exact: x.exact === true };
  }

  /** 把一次检索的 hits 收进视图：同一个编号后来的盖掉先来的（编号在对话里不变）。返回这次的编号列表。 */
  function collectHits(view, hits) {
    var nums = [];
    if (!view.hits) view.hits = {};
    (Array.isArray(hits) ? hits : []).forEach(function (x) {
      var h = normKbHit(x);
      if (!h || !h.n) return;
      view.hits[h.n] = h;
      if (nums.indexOf(h.n) < 0) nums.push(h.n);
    });
    return nums;
  }

  /** 一个对话里引用过的出处：所有写完的回答里出现过、并且确实有这一条的编号，按编号排。 */
  function citedSources(view) {
    var hits = (view && view.hits) || {}, seen = {};
    function has(n) { return !!hits[n]; }
    ((view && view.items) || []).forEach(function (it) {
      if (it.kind !== 'assistant' || !it.text || it.streaming) return;
      citedNums(it.text, has).forEach(function (n) { seen[n] = true; });
    });
    return Object.keys(seen).map(Number).sort(function (a, b) { return a - b; }).map(function (n) { return hits[n]; });
  }

  /** 一条出处的小标签：[{text, tone}]。标准答案、旧版、问答对、版本。 */
  function hitBadges(hit) {
    if (!hit) return [];
    var out = [];
    if (hit.standard) out.push({ text: '标准答案' + (hit.uses > 0 ? ' · 用过 ' + groupNum(hit.uses) + ' 次' : ''), tone: 'ok' });
    if (hit.superseded) out.push({ text: '旧版', tone: 'warn' });
    if (hit.kind === 'qa' && !hit.standard) out.push({ text: '问答对', tone: '' });
    if (hit.version_label && !hit.superseded) out.push({ text: hit.version_label, tone: '' });
    return out;
  }

  /** 旧版片段下面那句："现行的是《……》"。newer 可能已经带书名号。 */
  function newerText(hit) {
    if (!hit || !hit.superseded) return '';
    var n = str(hit.newer).trim();
    if (!n) return '这是旧版，资料库里有更新的版本';
    return '现行的是' + (/^《/.test(n) ? n : '《' + n + '》');
  }

  /* ---------------- 核对数字（规格 9.3）和反馈（9.5） ---------------- */

  /** grounding 事件的 payload → {cited, unknown, missing: {n: [数字]}, uncited: [数字], ok}。认不出返回 null。 */
  function normGrounding(p) {
    if (!p || typeof p !== 'object') return null;
    function nums(a) { return (Array.isArray(a) ? a : []).map(str).filter(Boolean); }
    var missing = {};
    (Array.isArray(p.checks) ? p.checks : []).forEach(function (c) {
      if (!c || typeof c !== 'object') return;
      var n = parseInt(c.n, 10), m = nums(c.missing);
      if (n > 0 && m.length) missing[n] = (missing[n] || []).concat(m.filter(function (x) { return (missing[n] || []).indexOf(x) < 0; }));
    });
    return { cited: (Array.isArray(p.cited) ? p.cited : []).map(Number).filter(function (n) { return n > 0; }),
             unknown: (Array.isArray(p.unknown_refs) ? p.unknown_refs : []).map(Number).filter(function (n) { return n > 0; }),
             missing: missing, uncited: nums(p.uncited_numbers), ok: p.ok !== false };
  }

  /** 有多少个数字没对上（同一个数字在几个出处里都没找到只算一次）。 */
  function groundingMissCount(g) {
    if (!g) return 0;
    var seen = {};
    Object.keys(g.missing).forEach(function (n) { g.missing[n].forEach(function (x) { seen[x] = true; }); });
    return Object.keys(seen).length;
  }

  /** 回答下面那一行小字的各段：["依据 3 处资料", "有 1 个数字没对上", "有 2 个数字没标出处"]。全对上时只有第一段。 */
  function basisParts(count, mode, g) {
    var out = [], head = basisText(count, mode);
    if (head) out.push({ text: head, tone: count > 0 ? '' : 'muted' });
    var miss = groundingMissCount(g);
    if (miss) out.push({ text: '有 ' + miss + ' 个数字没对上', tone: 'warn' });
    if (g && g.uncited.length) out.push({ text: '有 ' + g.uncited.length + ' 个数字没标出处', tone: 'warn' });
    return out;
  }

  /** 出处标签上的提醒："这个数字在出处里没找到：12.4%"。没有问题时是 ''。 */
  function citeWarn(g, n) {
    var m = g && g.missing[n];
    return m && m.length ? '这个数字在出处里没找到：' + m.join('、') : '';
  }

  /** feedback 事件 → 反馈状态 {rating, note}。rating 为 null 表示取消。 */
  function normFeedback(p) {
    if (!p || typeof p !== 'object') return null;
    var r = p.rating === 'up' || p.rating === 'down' ? p.rating : null;
    return { seq: parseInt(p.seq, 10) || 0, rating: r, note: str(p.note) };
  }

  /** 发消息的请求体里加 depth：只有开了"深入研究"才带 "deep"。 */
  function withDepth(body, deep) {
    var b = Object.assign({}, body);
    if (deep) b.depth = 'deep';
    return b;
  }

  /** 出处的一行：文件名 · 出处。 */
  function hitWhere(hit) {
    if (!hit) return '';
    return [hit.title, hit.loc || hit.heading].filter(Boolean).join(' · ');
  }

  /* ---------------- 资料库 ---------------- */

  /** GET /api/libraries 的一项 → 界面用的形状。 */
  function normLibrary(x) {
    if (!x || typeof x !== 'object' || !x.id) return null;
    var pr = x.progress && typeof x.progress === 'object' ? x.progress : {};
    function n(v) { return typeof v === 'number' && isFinite(v) && v >= 0 ? Math.round(v) : 0; }
    return {
      id: str(x.id), name: str(x.name) || '未命名资料库', note: str(x.note),
      sources: (Array.isArray(x.sources) ? x.sources : []).filter(function (s) { return s && s.path; })
        .map(function (s) { return { id: str(s.id), path: str(s.path), kind: str(s.kind) }; }),
      docs: n(x.docs), parts: n(x.parts), failed: n(x.failed), vectors: n(x.vectors),
      state: x.state === 'indexing' ? 'indexing' : 'ready',
      progress: { total: n(pr.total), done: n(pr.done), failed: n(pr.failed), current: str(pr.current) },
      updated: x.updated || null, synced: x.synced || null, builtin: x.builtin === true,
      // 第 1、5 节：卡片进度、主要语言、标签；公司共享的库（shared）带版本号，可以停用
      cards: normCards(x.cards), lang: str(x.lang), tags: normLibTags(x.tags),
      shared: x.shared === true, version: nonNeg(x.version), enabled: x.enabled !== false,
      v2: 'cards' in x || 'tags' in x
    };
  }

  /** 资料库列表。内置的"标准答案"库（builtin）不算用户的资料库：它在知识库页有自己的一栏，
   *  检索时后端总会带上它，所以不出现在资料库列表、范围选择和"已收录几份"里。 */
  function normalizeLibraries(d) {
    var list = Array.isArray(d) ? d : d && Array.isArray(d.libraries) ? d.libraries : [];
    return list.map(normLibrary).filter(function (l) { return l && !l.builtin; });
  }

  /** kb_progress 事件盖到资料库上（返回新对象，不改原来的）。做完了（done + failed ≥ total）就当已就绪。 */
  function applyKbProgress(lib, p) {
    if (!lib || !p || str(p.library_id) !== lib.id) return lib;
    var pr = { total: +p.total || 0, done: +p.done || 0, failed: +p.failed || 0, current: str(p.current) };
    var finished = pr.total > 0 && pr.done + pr.failed >= pr.total;
    return Object.assign({}, lib, { progress: pr, state: finished ? 'ready' : 'indexing' });
  }

  /** 资料库的状态文字：{text, tone}。tone：ok / busy / warn / empty。 */
  function libraryStateText(lib) {
    if (!lib) return { text: '', tone: '' };
    if (lib.state === 'indexing') {
      var p = lib.progress || {};
      return { text: p.total ? '正在收录 ' + Math.min(p.done + p.failed, p.total) + '/' + p.total : '正在收录…', tone: 'busy' };
    }
    if (lib.failed > 0) return { text: '有 ' + lib.failed + ' 个文件没收进去', tone: 'warn' };
    if (!lib.docs) return { text: lib.sources && lib.sources.length ? '没有能收录的文件' : '还没有文件', tone: 'empty' };
    return { text: '已就绪', tone: 'ok' };
  }

  /** 范围那一行的文字。selected 是选中的资料库编号；空表示全部。 */
  function scopeText(libs, selected) {
    libs = Array.isArray(libs) ? libs : [];
    var ids = (Array.isArray(selected) ? selected : []).filter(function (id) { return libs.some(function (l) { return l.id === id; }); });
    if (!ids.length || ids.length === libs.length) return '全部资料库';
    var names = ids.map(function (id) { return libs.filter(function (l) { return l.id === id; })[0].name; });
    if (names.length <= 2) return names.join('、');
    return names[0] + ' 等 ' + names.length + ' 个资料库';
  }

  /** 勾选 / 取消一个资料库。全部勾上等于"全部"（空数组）。 */
  function toggleScope(libs, selected, id) {
    var all = (Array.isArray(libs) ? libs : []).map(function (l) { return l.id; });
    var cur = (Array.isArray(selected) && selected.length ? selected : all).filter(function (x) { return all.indexOf(x) >= 0; });
    var next = cur.indexOf(id) >= 0 ? cur.filter(function (x) { return x !== id; }) : cur.concat([id]);
    if (!next.length) return cur;          // 至少留一个
    return next.length === all.length ? [] : all.filter(function (x) { return next.indexOf(x) >= 0; });
  }

  /** 新对话页范围下面的那行小字：已收录多少份 + 检索方式。semantic 是 featureGate('semantic_search') 的结果。 */
  function kbStatusText(libs, semantic) {
    libs = Array.isArray(libs) ? libs : [];
    var docs = 0, busy = null;
    libs.forEach(function (l) { docs += l.docs || 0; if (l.state === 'indexing' && !busy) busy = l; });
    var head = '已收录 ' + groupNum(docs) + ' 份资料';
    var busyText = busy ? '「' + busy.name + '」正在收录' + (busy.progress.total ? ' ' + Math.min(busy.progress.done + busy.progress.failed, busy.progress.total) + '/' + busy.progress.total : '') : '';
    var tail = '';
    if (semantic && semantic.ok === false) tail = semantic.fallback || semantic.reason || '语义检索没开，现在用关键词检索';
    return { head: head, busy: busyText, tail: tail, reason: semantic && semantic.ok === false ? semantic.reason : '' };
  }

  var DOC_STATUS = { ok: '已收录', failed: '没收进去', skipped: '跳过了' };
  var DOC_KIND = { doc: '文档', slides: 'PPT', sheet: '表格', text: '文本', qa: '问答对' };
  function docStatusText(s) { return DOC_STATUS[s] || (s ? str(s) : '已收录'); }
  function docKindText(k) { return DOC_KIND[k] || (k ? str(k) : ''); }

  /** 文档表的筛选：按名字（标题或路径里含这几个字）和状态（'' 表示全部）。 */
  function filterDocs(docs, q, status, tag) {
    var k = str(q).trim().toLowerCase();
    return (Array.isArray(docs) ? docs : []).filter(function (d) {
      if (!d) return false;
      if (status && (d.status || 'ok') !== status) return false;
      if (tag && strList(d.tags).indexOf(tag) < 0) return false;
      if (!k) return true;
      return (str(d.title) + ' ' + str(d.path) + ' ' + str(d.summary)).toLowerCase().indexOf(k) >= 0;
    });
  }

  /** 资料库的名字：按名字（不分大小写）或编号找。/范围 的参数用。 */
  function matchLibraries(libs, arg) {
    var a = str(arg).trim();
    if (!a) return null;
    if (/^(全部|所有|all)$/i.test(a)) return [];
    var whole = (libs || []).filter(function (l) { return l.name.toLowerCase() === a.toLowerCase(); })[0];
    if (whole) return [whole.id];
    var want = a.split(/[\s,，、]+/).filter(Boolean), out = [];
    for (var i = 0; i < want.length; i++) {
      var w = want[i].toLowerCase();
      var hit = (libs || []).filter(function (l) { return l.id === want[i] || l.name.toLowerCase() === w; })[0] ||
        ((libs || []).filter(function (l) { return l.name.toLowerCase().indexOf(w) >= 0; }).length === 1
          ? (libs || []).filter(function (l) { return l.name.toLowerCase().indexOf(w) >= 0; })[0] : null);
      if (!hit) return null;
      if (out.indexOf(hit.id) < 0) out.push(hit.id);
    }
    return out;
  }

  /** /api/capabilities 里的 embedding 段 → {engine, model, alive, dims, kb_engine} 或 null。 */
  function normEmbedding(e) {
    if (!e || typeof e !== 'object') return null;
    return { engine: str(e.engine), model: str(e.model), alive: e.alive === true ? true : e.alive === false ? false : null,
             dims: posNum(e.dims), kb_engine: str(e.kb_engine) };
  }

  var KB_ENGINE_TEXT = { langchain: 'LangChain（向量库 Chroma）', builtin: '内置（SQLite 加 numpy）' };
  function kbEngineText(k) { return KB_ENGINE_TEXT[k] || (k ? str(k) : '未知'); }

  /** 设置页"模型能力"下面那一行向量模型的文字。 */
  function embeddingLine(e) {
    if (!e) return '';
    if (!e.model || e.engine === 'off') return '没有接向量模型';
    return [e.model, e.alive === true ? '在线' : e.alive === false ? '连不上' : '', e.dims ? e.dims + ' 维' : ''].filter(Boolean).join(' · ');
  }

  /* ================================================================
   * 借鉴开源项目这一轮（docs/借鉴开源项目.md 第 7 节）的纯逻辑：
   * 文档卡片、检索范围（标签、指定文件、#）、带参数的模板、没答上来的问题、检索评测、工具组、公司共享目录
   * ================================================================ */

  function nonNeg(v) { return typeof v === 'number' && isFinite(v) && v >= 0 ? Math.round(v) : 0; }
  function strList(a) { return (Array.isArray(a) ? a : []).map(function (x) { return str(x).trim(); }).filter(Boolean); }

  /* ---------- 文档卡片（第 1 节） ---------- */

  var LANG_TEXT = { zh: '中文为主', en: '英文为主', mixed: '中英混合' };
  function langText(l) { return LANG_TEXT[str(l)] || ''; }

  /** 资料库的 cards：{total, ready, pending, failed}；没有这个字段返回 null（老后端）。 */
  function normCards(c) {
    if (!c || typeof c !== 'object') return null;
    // 真后端多给：extract（开头摘录的份数）、mode（model / extract / off）、reason（不是 model 时的原话）、writing（后台正在写）
    return { total: nonNeg(c.total), ready: nonNeg(c.ready), pending: nonNeg(c.pending), failed: nonNeg(c.failed),
             extract: nonNeg(c.extract), mode: str(c.mode), reason: str(c.reason), writing: c.writing === true ? true : c.writing === false ? false : null };
  }

  /** "摘要 180 / 212，后台在写"。全写完时"摘要都写好了"；没有卡片信息时 ''。 */
  function cardStatsText(c) {
    c = normCards(c);
    if (!c || !c.total) return '';
    // 模型写不了（演示模式、没接模型）或设置里关了：不说"后台在写"，只说现在有什么，原因另外一行（cardsReason）
    if (c.mode === 'off') return '文档摘要关着，只有目录和开头摘录';
    if (c.mode === 'extract') {
      var ex = c.extract || Math.max(0, c.total - c.ready);
      if (c.ready === 0 || ex >= c.total) return '摘要先用开头摘录';
      return '摘要 ' + groupNum(c.ready) + ' / ' + groupNum(c.total) + '，其余 ' + groupNum(ex) + ' 份先用开头摘录';
    }
    var head = '摘要 ' + groupNum(c.ready) + ' / ' + groupNum(c.total);
    var tail = [];
    if (c.pending > 0) tail.push(c.writing === false ? groupNum(c.pending) + ' 份等着写' : '后台在写');
    if (c.failed > 0) tail.push(groupNum(c.failed) + ' 份没写成');
    if (!tail.length && c.ready >= c.total) return '摘要都写好了';
    return head + (tail.length ? '，' + tail.join('，') : '');
  }

  /** 摘要不是模型在写时的原因（后端原话）；模型在写时是 ''。 */
  function cardsReason(c) {
    c = normCards(c);
    return c && c.mode && c.mode !== 'model' ? c.reason : '';
  }

  /** "后台在写"那个跳动的小点：只在模型真的在写时。 */
  function cardsBusy(c) {
    c = normCards(c);
    return !!c && c.pending > 0 && (c.mode === 'model' || !c.mode) && c.writing !== false;
  }

  /** kb_cards 事件盖到资料库上（返回新对象）。 */
  function applyKbCards(lib, p) {
    if (!lib || !p || str(p.library_id) !== lib.id) return lib;
    var old = lib.cards || { total: 0, ready: 0, pending: 0, failed: 0 };
    var c = { total: p.total != null ? nonNeg(p.total) : old.total, ready: p.ready != null ? nonNeg(p.ready) : old.ready,
              pending: p.pending != null ? nonNeg(p.pending) : old.pending, failed: p.failed != null ? nonNeg(p.failed) : old.failed };
    return Object.assign({}, lib, { cards: c });
  }

  var SUMMARY_BY_TEXT = { extract: '开头摘录', user: '你改过的', model: '' };
  function summaryByText(by) { return SUMMARY_BY_TEXT[str(by)] || ''; }

  /** 文档表里的一份文档：原来的字段原样保留，补上卡片的几项。hasCard 为假表示后端还没有文档卡片。 */
  function normDoc(x) {
    if (!x || typeof x !== 'object') return null;
    var d = Object.assign({}, x);
    d.id = str(x.id);
    d.summary = str(x.summary);
    d.summary_by = str(x.summary_by);
    d.card_state = str(x.card_state);
    d.tags = strList(x.tags);
    d.lang = str(x.lang);
    d.version_label = str(x.version_label);
    d.outline_count = nonNeg(x.outline_count);
    d.hasCard = 'summary' in x || 'card_state' in x || 'summary_by' in x;
    return d;
  }

  /** 摘要的第一行（文档表里灰字显示）。 */
  function summaryLine(d, n) {
    var s = str(d && d.summary).replace(/\r/g, '').split('\n').map(function (x) { return x.trim(); }).filter(Boolean)[0] || '';
    return clip(s, n || 80);
  }

  /** GET /api/kb/docs/<id>/card → 界面用的形状。认不出时返回 null。 */
  function normCard(d) {
    if (!d || typeof d !== 'object') return null;
    var doc = d.doc && typeof d.doc === 'object' ? d.doc : {};
    var outline = (Array.isArray(d.outline) ? d.outline : []).filter(function (o) { return o && (o.title || o.loc); }).map(function (o) {
      var lv = parseInt(o.level, 10);
      return { level: lv >= 1 && lv <= 6 ? lv : 1, title: str(o.title) || str(o.loc), loc: str(o.loc), section_id: str(o.section_id) };
    });
    return {
      doc: { id: str(doc.id), title: str(doc.title) || fileName(str(doc.path)) || '文档', path: str(doc.path), kind: str(doc.kind),
             library_id: str(doc.library_id), library: str(doc.library), version_label: str(doc.version_label), updated: doc.updated || null },
      summary: str(d.summary), summary_by: str(d.summary_by), keywords: strList(d.keywords), outline: outline,
      tags: strList(d.tags), lang: str(d.lang), card_state: str(d.card_state), card_at: d.card_at || null
    };
  }

  /** 卡片上摘要那块的状态说明。 */
  function cardStateText(c) {
    if (!c) return '';
    if (c.card_state === 'failed') return '摘要没写成。可以点"重新写摘要"再试一次。';
    if (c.card_state === 'outline' || (!c.summary && c.card_state !== 'ready')) return c.summary ? '后台还在写摘要，先显示开头摘录。' : '后台还在写摘要。';
    return '';
  }

  var TAG_MAX = 10, TAG_LEN = 20;

  /** 加一个标签：去掉首尾空白，1 到 20 个字，最多 10 个，重复的不加。返回 {tags, error}。 */
  function addTag(tags, raw) {
    var list = strList(tags);
    var t = str(raw).replace(/\s+/g, ' ').trim().replace(/^#+|^＃+/, '');
    if (!t) return { tags: list, error: '' };
    if (t.length > TAG_LEN) return { tags: list, error: '标签最多 ' + TAG_LEN + ' 个字。' };
    if (list.some(function (x) { return x.toLowerCase() === t.toLowerCase(); })) return { tags: list, error: '已经有这个标签了。' };
    if (list.length >= TAG_MAX) return { tags: list, error: '一份文件最多 ' + TAG_MAX + ' 个标签。' };
    return { tags: list.concat([t]), error: '' };
  }

  function removeTag(tags, t) { return strList(tags).filter(function (x) { return x !== t; }); }

  /** 资料库的标签：[{tag, docs}]，按文档数排，最多 30 个。 */
  function normLibTags(a) {
    return (Array.isArray(a) ? a : []).map(function (x) {
      if (typeof x === 'string') return { tag: x.trim(), docs: 0 };
      return x && typeof x === 'object' ? { tag: str(x.tag).trim(), docs: nonNeg(x.docs) } : null;
    }).filter(function (x) { return x && x.tag; }).sort(function (a, b) { return b.docs - a.docs; }).slice(0, 30);
  }

  /** 几个资料库的标签合在一起（同名的文档数相加）。范围菜单用。 */
  function mergeTags(libs) {
    var idx = {}, out = [];
    (Array.isArray(libs) ? libs : []).forEach(function (l) {
      (l && l.tags || []).forEach(function (t) {
        if (!(t.tag in idx)) { idx[t.tag] = out.length; out.push({ tag: t.tag, docs: 0 }); }
        out[idx[t.tag]].docs += t.docs;
      });
    });
    return out.sort(function (a, b) { return b.docs - a.docs; });
  }

  /* ---------- 检索范围：资料库、标签、指定文件（第 2.4 节） ---------- */

  /** {libraries, docs, tags} 都是字符串列表。 */
  function normScope(s) {
    s = s && typeof s === 'object' ? s : {};
    return { libraries: strList(s.libraries), docs: strList(s.docs), tags: strList(s.tags) };
  }

  function scopeEmpty(s) { s = normScope(s); return !s.libraries.length && !s.docs.length && !s.tags.length; }

  /** 发请求时带的范围：只带有值的几项。 */
  function scopeBody(s) {
    s = normScope(s);
    var b = {};
    if (s.libraries.length) b.libraries = s.libraries;
    if (s.docs.length) b.docs = s.docs;
    if (s.tags.length) b.tags = s.tags;
    return b;
  }

  /** 列表里加或去掉一项（返回新列表）。 */
  function toggleIn(list, x) {
    list = strList(list);
    return list.indexOf(x) >= 0 ? list.filter(function (y) { return y !== x; }) : list.concat([x]);
  }

  /** 输入框上方的小标签：[{kind: lib|tag|doc, id, label}]。资料库只在没选全部时列出。names：文档编号 → 文件名。 */
  function scopeChips(libs, s, names) {
    libs = Array.isArray(libs) ? libs : [];
    s = normScope(s);
    names = names || {};
    var out = [];
    var ids = s.libraries.filter(function (id) { return libs.some(function (l) { return l.id === id; }); });
    if (ids.length && ids.length < libs.length) {
      ids.forEach(function (id) { out.push({ kind: 'lib', id: id, label: libs.filter(function (l) { return l.id === id; })[0].name }); });
    }
    s.tags.forEach(function (t) { out.push({ kind: 'tag', id: t, label: '标签：' + t }); });
    s.docs.forEach(function (id) { out.push({ kind: 'doc', id: id, label: names[id] || '一份文件' }); });
    return out;
  }

  /** 范围按钮上的字："全部资料库"、"公司 SOP"、"标签：报销"、"差旅管理办法.docx"、"3 份文件"，几样用" · "连起来。 */
  function scopeLabel(libs, s, names) {
    s = normScope(s);
    names = names || {};
    var parts = [];
    var lt = scopeText(libs, s.libraries);
    if (lt !== '全部资料库' || (!s.tags.length && !s.docs.length)) parts.push(lt);
    if (s.tags.length) parts.push('标签：' + (s.tags.length <= 2 ? s.tags.join('、') : s.tags[0] + ' 等 ' + s.tags.length + ' 个'));
    if (s.docs.length === 1) parts.push(names[s.docs[0]] ? clip(fileName(names[s.docs[0]]), 24) : '1 份文件');
    else if (s.docs.length > 1) parts.push(s.docs.length + ' 份文件');
    return parts.join(' · ');
  }

  var HASH_CHARS = '#＃';

  /** 光标前面是不是正在写 "#文件名"：# 在开头或者前面是空白，# 到光标之间没有空白。
   *  返回 {start: # 的位置, end: 光标, q: # 后面已经写的字} 或 null。 */
  function hashQuery(value, caret) {
    var v = str(value);
    var end = typeof caret === 'number' && caret >= 0 && caret <= v.length ? caret : v.length;
    for (var i = end - 1; i >= 0; i--) {
      var ch = v.charAt(i);
      if (/\s/.test(ch)) return null;
      if (HASH_CHARS.indexOf(ch) >= 0) {
        if (i > 0 && !/\s/.test(v.charAt(i - 1))) return null;
        var q = v.slice(i + 1, end);
        if (q.length > 40) return null;
        return { start: i, end: end, q: q };
      }
    }
    return null;
  }

  /** 选好文件后把 "#文件名" 从输入框里去掉。返回 {value, caret}。 */
  function dropHashToken(value, tok) {
    var v = str(value);
    if (!tok) return { value: v, caret: v.length };
    var before = v.slice(0, tok.start), after = v.slice(tok.end);
    if (/\s$/.test(before) && /^\s/.test(after)) after = after.replace(/^\s+/, '');
    return { value: before + after, caret: before.length };
  }

  /** 按文件名、摘要、标签找文档。文件名命中的排前面。最多 limit 份（默认 30）。 */
  function searchDocs(docs, q, limit) {
    var k = str(q).trim().toLowerCase().replace(/\s+/g, '');
    var list = (Array.isArray(docs) ? docs : []).filter(function (d) { return d && d.id && (d.status || 'ok') === 'ok'; });
    var scored = list.map(function (d, i) {
      if (!k) return { d: d, i: i, s: 1 };
      var t = str(d.title || fileName(d.path)).toLowerCase();
      var s = matchScore(k, t);
      if (s >= 10 && t.indexOf(k) < 0) s = 8;          // 只是零散对上的字，排在摘要命中之后
      if (s < 50) {
        var rest = (str(d.summary) + ' ' + strList(d.tags).join(' ') + ' ' + str(d.path)).toLowerCase();
        if (rest.indexOf(k) >= 0) s = Math.max(s, 30);
      }
      return { d: d, i: i, s: s };
    }).filter(function (x) { return x.s > 0; });
    scored.sort(function (a, b) { return b.s - a.s || a.i - b.i; });
    return scored.slice(0, limit || 30).map(function (x) { return x.d; });
  }

  /* ---------- 带参数的模板（第 6 节） ---------- */

  var TPL_TYPES = ['text', 'month', 'date', 'number', 'file', 'library', 'choice'];
  var TPL_RE = /\{\{\s*([^{}]*?)\s*\}\}/g;

  /** {{…}} 里的一段 → {name, type, options, required}；写法不对返回 null。
   *  月份 / 月份:month / 部门:choice=销售|市场 / 备注? / 备注?:text */
  function parseTplSlot(raw) {
    var s = str(raw).trim();
    if (!s) return null;
    var name = s, spec = '';
    var c = s.search(/[:：]/);
    if (c >= 0) { name = s.slice(0, c).trim(); spec = s.slice(c + 1).trim(); }
    var optional = false;
    if (/[?？]$/.test(name)) { optional = true; name = name.slice(0, -1).trim(); }
    if (/[?？]$/.test(spec)) { optional = true; spec = spec.slice(0, -1).trim(); }
    if (!name || name.length > 30 || /[{}]/.test(name)) return null;
    var type = 'text', options = [];
    if (spec) {
      var eq = spec.indexOf('=');
      var t = (eq >= 0 ? spec.slice(0, eq) : spec).trim().toLowerCase();
      if (TPL_TYPES.indexOf(t) < 0) return null;
      type = t;
      if (eq >= 0) options = spec.slice(eq + 1).split(/[|｜]/).map(function (x) { return x.trim(); }).filter(Boolean);
      if (type === 'choice' && !options.length) return null;
    }
    return { name: name, label: name, type: type, options: options, required: !optional, 'default': '' };
  }

  /** 从 prompt 里解析出空（按出现顺序，同名的只算一次）。 */
  function parseTplParams(prompt) {
    var out = [], seen = {}, m;
    var s = str(prompt);
    TPL_RE.lastIndex = 0;
    while ((m = TPL_RE.exec(s))) {
      var p = parseTplSlot(m[1]);
      if (!p || seen[p.name]) continue;
      seen[p.name] = true;
      out.push(p);
    }
    return out;
  }

  /** 后端给的 params（形如 [{name, label?, type?, options?, default?, required?}]）整理一下。 */
  function normTplParams(list) {
    var out = [], seen = {};
    (Array.isArray(list) ? list : []).forEach(function (p) {
      if (!p || typeof p !== 'object') return;
      var name = str(p.name).trim();
      if (!name || seen[name]) return;
      seen[name] = true;
      var type = TPL_TYPES.indexOf(str(p.type)) >= 0 ? str(p.type) : 'text';
      var options = strList(p.options);
      if (type === 'choice' && !options.length) type = 'text';
      out.push({ name: name, label: str(p.label).trim() || name, type: type, options: options,
                 required: p.required !== false, 'default': p['default'] == null ? '' : str(p['default']) });
    });
    return out;
  }

  /** 一条模板的空：有 params 以它为准，没有就从 prompt 里解析。 */
  function tplParams(t) {
    if (!t) return [];
    if (Array.isArray(t.params) && t.params.length) return normTplParams(t.params);
    return parseTplParams(t.prompt);
  }

  /** 填好的值放进 prompt。可选的空没填：连同它前面紧挨着的标点一起去掉。填进去的值是普通文字，不再解析里面的 {{}}。 */
  function renderTpl(prompt, values) {
    values = values || {};
    var s = str(prompt);
    return s.replace(/([，,、；;：:。．.\s]*)\{\{\s*([^{}]*?)\s*\}\}/g, function (all, lead, inner) {
      var p = parseTplSlot(inner);
      if (!p) return all;
      var v = values[p.name] == null ? '' : str(values[p.name]).trim();
      if (v) return lead + v;
      if (!p.required) return '';
      return lead + all.slice(lead.length);
    }).replace(/[ \t]{2,}/g, ' ');
  }

  /** 必填没填的空（名字列表）。 */
  function tplMissing(params, values) {
    values = values || {};
    return (Array.isArray(params) ? params : []).filter(function (p) {
      return p.required && !str(values[p.name]).trim();
    }).map(function (p) { return p.name; });
  }

  /** 写法不对的 {{…}}（"我的模板"编辑时提示）：返回原样的几段。 */
  function tplSlotProblems(prompt) {
    var out = [], m, s = str(prompt);
    TPL_RE.lastIndex = 0;
    while ((m = TPL_RE.exec(s))) if (!parseTplSlot(m[1]) && out.indexOf(m[0]) < 0) out.push(m[0]);
    return out;
  }

  /** "解析出了几个空"那一行：["月份（年月）", "部门（选一个：销售 / 市场）", "备注（可不填）"]。 */
  function tplParamsText(params) {
    return (Array.isArray(params) ? params : []).map(function (p) {
      var t = p.type === 'choice' ? '选一个：' + p.options.join(' / ') : tplTypeText(p.type);
      return p.label + '（' + t + (p.required ? '' : '，可不填') + '）';
    });
  }

  var TPL_TYPE_TEXT = { text: '文字', month: '年月', date: '日期', number: '数字', file: '文件夹里的文件', library: '资料库', choice: '选一个' };
  function tplTypeText(t) { return TPL_TYPE_TEXT[t] || '文字'; }

  var TPL_SOURCE_TEXT = { builtin: '', user: '我的', shared: '公司' };
  function tplSourceText(s) { return TPL_SOURCE_TEXT[str(s)] || ''; }

  /* ---------- 没答上来的问题、标准答案、检索评测（第 4 节） ---------- */

  var GAP_REASON = { no_hit: '资料里没找到', down: '回答被标了不对' };
  function gapReasonText(r) { return GAP_REASON[str(r)] || (r ? str(r) : ''); }

  function normGap(x) {
    if (!x || typeof x !== 'object' || x.id == null || !str(x.question).trim()) return null;
    return { id: str(x.id), question: str(x.question).trim(), reason: str(x.reason), count: Math.max(1, nonNeg(x.count)),
             first_ts: x.first_ts || null, last_ts: x.last_ts || null, session_id: str(x.session_id), seq: parseInt(x.seq, 10) || 0,
             status: str(x.status) || 'open', note: str(x.note), answer_id: str(x.answer_id) };
  }

  /** 按次数、再按最近一次排。 */
  function normGaps(d) {
    var list = Array.isArray(d) ? d : d && Array.isArray(d.gaps) ? d.gaps : d && Array.isArray(d.items) ? d.items : [];
    return list.map(normGap).filter(Boolean).sort(function (a, b) { return b.count - a.count || (b.last_ts || 0) - (a.last_ts || 0); });
  }

  /** 标准答案下面那句"用过 N 次"。后端没有 hits 字段时是 ''。 */
  function answerUseText(a) {
    if (!a || typeof a.hits !== 'number') return '';
    return a.hits > 0 ? '用过 ' + groupNum(a.hits) + ' 次' : '还没用过';
  }

  /** 应命中的文件：分号、顿号、换行分开。 */
  function parseExpect(text) {
    var seen = {};
    return str(text).split(/[;；、\n]+/).map(function (x) { return x.trim(); }).filter(function (x) {
      if (!x || seen[x]) return false; seen[x] = true; return true;
    });
  }

  function normEvalRun(r) {
    if (!r || typeof r !== 'object') return null;
    return { id: str(r.id), ts: r.ts || null, mode: str(r.mode) || 'auto', total: nonNeg(r.total), hit1: nonNeg(r.hit1), hit3: nonNeg(r.hit3),
             hit5: nonNeg(r.hit5), ms: typeof r.ms === 'number' ? r.ms : null,
             hasMisses: Array.isArray(r.misses),      // 列表里的历次结果不带明细，只有刚跑完的这次带
             misses: (Array.isArray(r.misses) ? r.misses : []).filter(Boolean).map(function (m) {
               return { question: str(m.question), expect: strList(m.expect), got: strList(m.got) };
             }) };
  }

  /** GET /api/kb/eval → {items, runs}，runs 新的在前。 */
  function normEval(d) {
    d = d && typeof d === 'object' ? d : {};
    var items = (Array.isArray(d.items) ? d.items : []).filter(function (x) { return x && x.id != null && str(x.question).trim(); })
      .map(function (x) { return { id: str(x.id), question: str(x.question).trim(), expect: strList(x.expect), note: str(x.note) }; });
    var runs = (Array.isArray(d.runs) ? d.runs : []).map(normEvalRun).filter(Boolean)
      .sort(function (a, b) { return (b.ts || 0) - (a.ts || 0); });
    return { items: items, runs: runs };
  }

  /** 命中率（整数百分比）。 */
  function evalPct(run, k) {
    if (!run || !run.total) return 0;
    return Math.round((run['hit' + k] || 0) / run.total * 100);
  }

  /** 和上一次比：[{k, pct, delta}]，delta 是百分点，没有上一次时为 null。 */
  function evalCompare(cur, prev) {
    return [1, 3, 5].map(function (k) {
      var pct = evalPct(cur, k);
      return { k: k, pct: pct, delta: prev && prev.total ? pct - evalPct(prev, k) : null };
    });
  }

  function deltaText(d) {
    if (d == null) return '';
    if (d > 0) return '比上次高 ' + d + ' 点';
    if (d < 0) return '比上次低 ' + (-d) + ' 点';
    return '和上次一样';
  }

  var EVAL_MODE_TEXT = { auto: '自动', keyword: '只用了关键词', hybrid: '关键词 + 语义' };
  function evalModeText(m) { return EVAL_MODE_TEXT[str(m)] || str(m); }

  /* ---------- 工具组（第 3 节） ---------- */

  var TOOLSET_NAMES = { core: '常用工具', files: '整理文件', excel: '处理 Excel', analysis: '查询和比对表格', docs: '生成 Word 和 PPT', kb: '查公司资料库',
    kb_add: '收录资料', memory: '记忆和历史', browser: '操作网页', desktop: '操作桌面程序', web: '联网搜索', code: '运行分析代码', auto: '定时任务和技能' };

  /** 组的中文名。list 是 GET /api/toolsets 的结果（有就用它的 name）；mcp:名字 → "外部系统：名字"。 */
  function toolsetName(id, list) {
    id = str(id);
    var hit = (Array.isArray(list) ? list : []).filter(function (g) { return g && str(g.id) === id; })[0];
    if (hit && str(hit.name)) return str(hit.name);
    if (/^mcp:/.test(id)) return '外部系统：' + id.slice(4);
    return TOOLSET_NAMES[id] || id;
  }

  /** /status 的 toolset → 整理后的形状；没有返回 null。 */
  function normToolset(t) {
    if (!t || typeof t !== 'object') return null;
    return { lazy: t.lazy === true, loaded: strList(t.loaded), unloaded: strList(t.unloaded),
             chars: typeof t.chars === 'number' ? t.chars : null, chars_all: typeof t.chars_all === 'number' ? t.chars_all : null,
             // 真后端多给 groups：[{id, name, desc, tools, loaded, why, chars}]，组名以它为准
             groups: (Array.isArray(t.groups) ? t.groups : []).filter(function (g) { return g && g.id; }) };
  }

  function approxChars(n) {
    if (typeof n !== 'number' || !isFinite(n) || n <= 0) return '';
    return '约 ' + groupNum(n < 1000 ? n : Math.round(n / 100) * 100) + ' 字符';
  }

  /** "带了 5 组（约 6,100 字符），没带 4 组"；按需加载关着时 "全部带上（约 12,900 字符）"。 */
  function toolsetLine(t) {
    t = normToolset(t);
    if (!t) return '';
    var c = approxChars(t.chars);
    if (!t.lazy) return '全部带上' + (c || approxChars(t.chars_all) ? '（' + (c || approxChars(t.chars_all)) + '）' : '');
    return '带了 ' + t.loaded.length + ' 组' + (c ? '（' + c + '）' : '') + (t.unloaded.length ? '，没带 ' + t.unloaded.length + ' 组' : '');
  }

  /* ---------- 公司共享目录、被锁住的设置（第 5 节） ---------- */

  /** key 是不是被锁住了：列表里有它本身或它的上一级（"kb" 锁住 "kb.top_k"）。 */
  function isLocked(locked, key) {
    key = str(key);
    return strList(locked).some(function (k) { return k === key || key.indexOf(k + '.') === 0; });
  }

  /** 这一节里被锁住的键（它本身、上一级、或者下面的某一项）。 */
  function lockedUnder(locked, prefix) {
    prefix = str(prefix);
    return strList(locked).filter(function (k) { return k === prefix || k.indexOf(prefix + '.') === 0 || prefix.indexOf(k + '.') === 0; });
  }

  var SETTING_KEY_TEXT = { endpoints: '模型服务', embedding: '向量模型', rerank: '重排模型', asr: '语音识别', web: '联网',
    browser: '操作网页', 'browser.allowed_domains': '允许打开的网站', 'browser.blocked_domains': '不许打开的网站', kb: '资料库检索',
    'kb.top_k': '每次检索取几条', 'kb.min_cover': '关键词检索的门槛', 'kb.min_sim': '语义检索的门槛', 'kb.cards': '文档摘要',
    tools: '工具', 'tools.lazy': '工具按需加载', 'tools.loop_guard': '重复调用检测', 'ui.theme': '主题', approval_mode: '批准方式',
    mcp_servers: '连接器', default_folder: '默认文件夹', shared: '共享目录', 'model.caps': '模型能力' };
  /** 设置项的中文名；不认识的原样返回。 */
  function settingKeyText(k) { k = str(k); return SETTING_KEY_TEXT[k] || (SETTING_KEY_TEXT[k.split('.')[0]] ? SETTING_KEY_TEXT[k.split('.')[0]] + '（' + k + '）' : k); }

  var SHARED_LIB_STATE = { ok: '已是最新', updating: '正在更新', outdated: '有新版本', error: '同步出错' };
  var VECTORS_TEXT = { ok: '语义检索可用', mismatch: '向量模型和本机不一样，只用关键词', none: '只有关键词' };

  function normSharedLib(x) {
    if (!x || typeof x !== 'object' || !x.id) return null;
    var state = SHARED_LIB_STATE[str(x.state)] ? str(x.state) : 'ok';
    return { id: str(x.id), name: str(x.name) || '资料库', version: nonNeg(x.version), local_version: nonNeg(x.local_version), state: state,
             docs: nonNeg(x.docs), parts: nonNeg(x.parts), vectors: str(x.vectors), built: x.built || null,
             vectors_why: str(x.vectors_why), error: str(x.error), enabled: x.enabled !== false };
  }

  /** GET /api/shared → 整理后的形状。 */
  function normShared(d) {
    d = d && typeof d === 'object' ? d : {};
    var st = d.settings && typeof d.settings === 'object' ? d.settings : {};
    return {
      dir: str(d.dir), configured: d.configured === true || (d.configured !== false && !!str(d.dir)), ok: d.ok === true, error: str(d.error),
      name: str(d.name), contact: str(d.contact), writable: d.writable === true, checked: d.checked || null, synced: d.synced || null,
      skills: nonNeg(d.skills), templates: nonNeg(d.templates),
      locked: strList(st.locked), defaults: strList(st.defaults),
      ignored: (Array.isArray(st.ignored) ? st.ignored : []).filter(function (x) { return x && x.key; }).map(function (x) { return { key: str(x.key), why: str(x.why) }; }),
      libraries: (Array.isArray(d.libraries) ? d.libraries : []).map(normSharedLib).filter(Boolean),
      notes: strList(d.notes), checking: d.checking === true,
      sync_minutes: typeof d.sync_minutes === 'number' ? d.sync_minutes : null
    };
  }

  /** 共享目录的一句话状态：{text, tone}。 */
  function sharedStateText(s, nowMs) {
    if (!s || !s.configured) return { text: '没有设置。各台电脑各用各的。', tone: 'empty' };
    if (s.checking) return { text: '正在检查…', tone: 'busy' };
    if (s.ok) return { text: '连上了' + (s.name ? '：' + s.name : ''), tone: 'ok' };
    var when = s.synced ? '，现在用的是上次同步的内容（' + formatTime(s.synced, nowMs) + '）' : '';
    return { text: '连不上' + when + (s.error ? '。' + s.error : ''), tone: 'warn' };
  }

  /** 一个共享资料库的版本那一行："第 7 版"、"本机第 6 版，有第 7 版"。 */
  function sharedLibText(l) {
    if (!l) return '';
    if (l.state === 'updating') return '正在更新到第 ' + l.version + ' 版';
    if (l.state === 'outdated' && l.local_version && l.local_version !== l.version) return '本机第 ' + l.local_version + ' 版，有第 ' + l.version + ' 版';
    if (l.state === 'error') return '第 ' + (l.local_version || l.version) + ' 版 · 同步出错';
    return '第 ' + (l.version || l.local_version) + ' 版';
  }

  var Pure = {
    escapeHtml: escapeHtml, unescapeHtml: unescapeHtml, safeUrl: safeUrl, stripThink: stripThink,
    renderInline: renderInline, renderMarkdown: renderMarkdown,
    toolInfo: toolInfo, riskLabel: riskLabel, parseArgs: parseArgs, clip: clip,
    statusText: statusText, needsAttention: needsAttention, isActive: isActive,
    newView: newView, reduceEvent: reduceEvent, applyPending: applyPending, buildView: buildView, attention: attention,
    applyListEvent: applyListEvent, groupTemplates: groupTemplates,
    formatTime: formatTime, relativeTime: relativeTime, groupSessions: groupSessions, listTime: listTime,
    formatElapsed: formatElapsed, greeting: greeting, formatSize: formatSize, fileName: fileName, prettyResult: prettyResult,
    matchScore: matchScore, filterCommands: filterCommands, tokenFrom: tokenFrom, apiUrl: apiUrl,
    resample: resample, encodeWav: encodeWav, concatSamples: concatSamples, insertAtCursor: insertAtCursor,
    formatDuration: formatDuration, meterLevel: meterLevel, micErrorKind: micErrorKind, httpErrorKind: httpErrorKind,
    voiceError: voiceError, isVoiceShortcut: isVoiceShortcut, kernelLabel: kernelLabel,
    originLabel: originLabel, targetLabel: targetLabel, memoryNoteText: memoryNoteText, usageText: usageText, usagePct: usagePct,
    shortDate: shortDate, normalizeMemories: normalizeMemories, describeLog: describeLog, normalizeLog: normalizeLog,
    normalizeSkills: normalizeSkills, searchQuery: searchQuery, normalizeHits: normalizeHits, highlightParts: highlightParts,
    searchStatusText: searchStatusText,
    apiErrorMessage: apiErrorMessage, dispositionName: dispositionName, exportUrl: exportUrl, MISSING_API: MISSING_API,
    isStep: isStep, foldSteps: foldSteps, groupTimeline: groupTimeline, normalizeFiles: normalizeFiles, attachState: attachState,
    sendMode: sendMode, defaultWorkspaceId: defaultWorkspaceId, startWorkspace: startWorkspace, folderList: folderList,
    pickSuggestions: pickSuggestions, isFileDrag: isFileDrag, uploadErrorText: uploadErrorText, uploadUrl: uploadUrl,
    splitFiles: splitFiles, uploadLimit: uploadLimit, tooBigText: tooBigText, samePath: samePath, needsOnboard: needsOnboard,
    stepsSig: stepsSig, filesSig: filesSig,
    normalizeModes: normalizeModes, pickMode: pickMode, sessionMode: sessionMode, sessionsForMode: sessionsForMode,
    parseHash: parseHash, buildHash: buildHash, withMode: withMode, navItems: navItems,
    splitCites: splitCites, stripCode: stripCode, citedNums: citedNums, basisText: basisText, normVia: normVia, viaLabel: viaLabel,
    normKbHit: normKbHit, collectHits: collectHits, citedSources: citedSources, hitWhere: hitWhere,
    hitBadges: hitBadges, newerText: newerText, normGrounding: normGrounding, groundingMissCount: groundingMissCount,
    basisParts: basisParts, citeWarn: citeWarn, normFeedback: normFeedback, withDepth: withDepth,
    normLibrary: normLibrary, normalizeLibraries: normalizeLibraries, applyKbProgress: applyKbProgress,
    libraryStateText: libraryStateText, scopeText: scopeText, toggleScope: toggleScope, kbStatusText: kbStatusText,
    docStatusText: docStatusText, docKindText: docKindText, filterDocs: filterDocs, matchLibraries: matchLibraries,
    normEmbedding: normEmbedding, kbEngineText: kbEngineText, embeddingLine: embeddingLine,
    langText: langText, normCards: normCards, cardStatsText: cardStatsText, cardsReason: cardsReason, cardsBusy: cardsBusy, applyKbCards: applyKbCards, summaryByText: summaryByText,
    normDoc: normDoc, summaryLine: summaryLine, normCard: normCard, cardStateText: cardStateText, addTag: addTag, removeTag: removeTag,
    normLibTags: normLibTags, mergeTags: mergeTags, normScope: normScope, scopeEmpty: scopeEmpty, scopeBody: scopeBody, toggleIn: toggleIn,
    scopeChips: scopeChips, scopeLabel: scopeLabel, hashQuery: hashQuery, dropHashToken: dropHashToken, searchDocs: searchDocs,
    parseTplSlot: parseTplSlot, parseTplParams: parseTplParams, normTplParams: normTplParams, tplParams: tplParams, renderTpl: renderTpl,
    tplMissing: tplMissing, tplTypeText: tplTypeText, tplSourceText: tplSourceText, tplSlotProblems: tplSlotProblems, tplParamsText: tplParamsText,
    gapReasonText: gapReasonText, normGap: normGap, normGaps: normGaps, answerUseText: answerUseText, parseExpect: parseExpect,
    normEval: normEval, normEvalRun: normEvalRun, evalPct: evalPct, evalCompare: evalCompare, deltaText: deltaText, evalModeText: evalModeText,
    toolsetName: toolsetName, normToolset: normToolset, toolsetLine: toolsetLine, TOOLSET_CACHE: TOOLSET_CACHE,
    isLocked: isLocked, lockedUnder: lockedUnder, settingKeyText: settingKeyText, normShared: normShared, sharedStateText: sharedStateText, sharedLibText: sharedLibText,
    SLASH_COMMANDS: SLASH_COMMANDS, parseSlash: parseSlash, slashAvailable: slashAvailable, slashCommands: slashCommands,
    filterSlash: filterSlash, findSlash: findSlash, resolveSlash: resolveSlash, slashFill: slashFill, messageBody: messageBody,
    matchMode: matchMode, matchTheme: matchTheme, compactText: compactText, groupNum: groupNum, statusRows: statusRows,
    normalizeCaps: normalizeCaps, capsFromError: capsFromError, featureGate: featureGate, modelRows: modelRows,
    contextHint: contextHint, viaText: viaText, featureLines: featureLines, normalizeDomain: normalizeDomain, addDomain: addDomain,
    displayOf: displayOf, mergeDisplay: mergeDisplay, stepText: stepText, alwaysOption: alwaysOption,
    UPLOAD_MISSING: UPLOAD_MISSING,
    VOICE_RATE: VOICE_RATE, SILENCE_PEAK: SILENCE_PEAK
  };

  if (typeof module !== 'undefined' && module.exports) module.exports = Pure;
  if (typeof window === 'undefined' || typeof document === 'undefined') return;

  /* ================================================================
   * 二、界面
   * ================================================================ */

  var MODES = [
    { id: 'manual', label: '每步都问', desc: '每个动作都先问你。' },
    { id: 'auto', label: '自动', desc: '当前文件夹里读文件、新建文件直接做；读文件夹以外的文件、覆盖、删除、运行代码、对外发送先问你。' },
    { id: 'full', label: '全放行', desc: '能直接读这台电脑上的任何文件（存密码、密钥的地方除外）；除了删除，其他都直接做。' }
  ];

  var PAGES = {
    kb: { title: '资料', sub: '导入后，Agent 能检索这些文档，回答时会注明出处。' },
    memory: { title: '记忆', sub: 'Agent 每次开始对话时会读这些内容。不对的可以改，不要的可以删。' },
    schedule: { title: '定时', sub: '到点自动新开一个对话去做。结果在对话列表里看。' },
    skills: { title: '技能', sub: '技能是做某类事的固定做法。遇到对应的事情，Agent 会先读说明再动手。' },
    settings: { title: '设置', sub: '外观、批准方式、文件夹和语音。技术项在最后的「高级」里。' }
  };

  var S = {
    token: '',
    info: {},
    workspaces: [],
    wsId: null,
    sessions: [],
    sid: null,
    view: null,
    loading: false,
    buffer: [],
    route: 'home',
    templates: [],
    tplRole: 0,
    lastSeq: 0,
    es: null,
    esFails: 0,
    esTimer: null,
    rendered: {},      // key → {el, v, dep}
    open: {},          // 展开的步骤
    images: { home: [], composer: [] },
    files: { home: [], composer: [] },   // 输入框上方的附件 {name, size, state: uploading|done|error, path, error, file, ws}
    composerSid: null,  // 对话页输入框里的附件属于哪个对话；换对话时清掉，免得串到别的对话里
    defaultWs: null,    // 默认空间的编号；旧版后端没有时是 null
    rightOpen: false,   // 宽屏时右栏（计划与成品）是否打开，默认收起
    tplMore: false,     // 新对话页是否展开了全部示例
    picking: false,     // 系统的"选择文件夹"窗口正开着
    noPicker: false,    // 这台机器弹不了选择窗口，直接用手工表单
    uploadMissing: false, // 后端没有上传接口
    busy: {},
    preview: null,
    dirty: {},
    rafPending: false,
    lastRenderError: 0,
    unknownSeen: {},
    rightSig: null,
    bannerSig: null,
    memUndo: {},       // 记忆改动编号 → {state: busy|done|error, msg}，时间线和记忆页共用
    suggestSent: {},   // 已经点过"存成技能"的提示
    caps: null,        // GET /api/capabilities 整理后的结果；老后端没有这个接口时是 {missing: true}，还没取到是 null
    modes: [],         // 两种工作模式（/api/state 的 modes 整理后）；老后端是 []，界面一切照旧
    mode: '',          // 当前模式 id；老后端是 ''
    tplBy: {},         // 模式 → 建议（模板）
    libs: null,        // 资料库列表（normalizeLibraries）；还没取是 null；老后端没有接口时是 []，libsMissing 为真
    libsMissing: false,
    libsAt: 0,
    scope: [],         // 新对话页选中的资料库；空表示全部
    scopeDocs: [],     // 新对话页"指定文件"选中的文档编号（第 2.4 节）
    scopeTags: [],     // 新对话页按标签选的范围
    docNames: {},      // 文档编号 → 文件名（范围小标签上显示用）
    shared: null,      // GET /api/shared 整理后的结果；老后端是 {missing: true}
    sharedAt: 0,
    tplForm: { home: null, composer: null },   // 输入框上方展开的模板表单 {tpl, params, values, errors}
    deep: { home: false, composer: false },   // "深入研究"开关，发完一条自动关
    srcFocus: 0,       // 来源面板里定位到的编号
    kbLive: null,      // 知识库页收进度事件的回调
    lastKbEvent: 0,
    search: { q: '', seq: 0, timer: null, loading: false, hits: null, note: '', error: '', ws: null }
  };

  function $(id) { return document.getElementById(id); }

  /** 建元素。文字一律走 textContent；不提供任何写 HTML 的入口。 */
  function h(tag, attrs) {
    var el = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v === undefined || v === null || v === false) return;
        if (k === 'class') el.className = v;
        else if (k === 'text') el.textContent = str(v);
        else if (k === 'on') Object.keys(v).forEach(function (ev) { el.addEventListener(ev, guard(v[ev])); });
        else if (k === 'dataset') Object.keys(v).forEach(function (d) { el.dataset[d] = v[d]; });
        else if (k === 'hidden') el.hidden = !!v;
        else if (k === 'disabled') el.disabled = !!v;
        else if (k === 'value') el.value = v;
        else if (k === 'checked') el.checked = !!v;
        else el.setAttribute(k, v === true ? '' : String(v));
      });
    }
    for (var i = 2; i < arguments.length; i++) append(el, arguments[i]);
    return el;
  }

  function append(el, c) {
    if (c === null || c === undefined || c === false) return;
    if (Array.isArray(c)) { c.forEach(function (x) { append(el, x); }); return; }
    el.appendChild(typeof c === 'object' && c.nodeType ? c : document.createTextNode(str(c)));
  }

  function clear(el) { if (el) while (el.firstChild) el.removeChild(el.firstChild); return el; }
  function show(el, yes) { if (el) el.hidden = !yes; }
  function setText(el, t) { if (el) el.textContent = str(t); }

  /** 唯一使用 innerHTML 的地方：内容来自 renderMarkdown，已经全部转义。 */
  function setMarkdown(el, text) {
    if (!el) return el;
    el.innerHTML = renderMarkdown(text);
    var links = el.querySelectorAll('a');
    for (var i = 0; i < links.length; i++) {
      if (!safeUrl(links[i].getAttribute('href') || '')) { links[i].removeAttribute('href'); }
      links[i].setAttribute('target', '_blank');
      links[i].setAttribute('rel', 'noopener noreferrer');
    }
    return el;
  }

  /* ---------------- 出错提示 ---------------- */

  function toast(msg, kind, action) {
    var box = $('toasts');
    if (!box) return;
    var el = h('div', { class: 'toast toast-' + (kind || 'error'), role: kind === 'info' ? 'status' : 'alert' },
      h('span', { class: 'toast-text', text: msg }));
    if (action) {
      el.appendChild(h('button', { class: 'btn btn-sm', type: 'button', text: action.label,
        on: { click: function () { remove(); return action.run(); } } }));
    }
    el.appendChild(h('button', { class: 'toast-x', type: 'button', 'aria-label': '关闭', text: '×', on: { click: function () { remove(); } } }));
    function remove() { if (el.parentNode) el.parentNode.removeChild(el); }
    box.appendChild(el);
    while (box.children.length > 4) box.removeChild(box.firstChild);
    setTimeout(remove, kind === 'info' ? 5000 : 9000);
  }

  /** 一直显示的提示（等系统窗口时用），返回关掉它的函数。 */
  function pickNote(msg) {
    var box = $('toasts');
    if (!box) return function () {};
    var el = h('div', { class: 'toast toast-info', role: 'status' }, h('span', { class: 'toast-text', text: msg }));
    box.appendChild(el);
    return function () { if (el.parentNode) el.parentNode.removeChild(el); };
  }

  /** 包一层：异常变成界面提示，不让页面卡死。 */
  function guard(fn) {
    return function () {
      try {
        var r = fn.apply(this, arguments);
        if (r && typeof r.then === 'function') r.then(null, report);
        return r;
      } catch (e) { report(e); }
    };
  }

  function report(e) {
    if (window.console) console.error(e);
    toast((e && e.message) || String(e) || '出了点问题');
  }

  /* ---------------- 接口 ---------------- */

  function api(method, path, body) {
    var opt = { method: method, headers: { 'X-Token': S.token }, cache: 'no-store' };
    if (body !== undefined) {
      opt.headers['Content-Type'] = 'application/json';
      opt.body = JSON.stringify(body);
    }
    return fetch(path, opt).then(function (r) {
      return r.text().then(function (txt) {
        var data = null;
        try { data = txt ? JSON.parse(txt) : null; } catch (e) { data = null; }
        if (!r.ok) {
          var msg = apiErrorMessage(r.status, data);
          var err = new Error(msg);
          err.status = r.status;
          err.code = data && data.code ? str(data.code) : '';
          err.data = data;
          err.missing = msg === MISSING_API;   // 程序还是旧版本，没有这个接口
          throw err;
        }
        return data;
      });
    }, function () {
      throw new Error('连不上 Agent 程序。它可能已经关闭，请重新运行启动脚本。');
    });
  }

  function ws() {
    for (var i = 0; i < S.workspaces.length; i++) if (S.workspaces[i].id === S.wsId) return S.workspaces[i];
    return null;
  }

  function fileUrl(path, download) {
    return apiUrl('/api/file', { workspace: S.wsId, path: path, download: download ? 1 : '', t: S.token });
  }

  /* ---------------- 启动 ---------------- */

  function boot() {
    var t = tokenFrom(location.search);
    try {
      if (t) sessionStorage.setItem('ca.token', t);
      else t = sessionStorage.getItem('ca.token') || '';
    } catch (e) { /* 隐私模式下 sessionStorage 可能不可用 */ }
    if (location.search.indexOf('t=') >= 0 && history.replaceState) {
      var rest = location.search.replace(/^\?/, '').split('&').filter(function (kv) { return kv && kv.indexOf('t=') !== 0; });
      history.replaceState(null, '', location.pathname + (rest.length ? '?' + rest.join('&') : '') + location.hash);
    }
    if (!t) { noToken(); return; }
    S.token = t;
    try { S.rightOpen = localStorage.getItem('ca.right') === '1'; } catch (e) { S.rightOpen = false; }
    bindStatic();
    loadState().then(function () {
      show($('boot'), false);
      show($('app'), true);
      connectEvents();
      loadCaps();
      loadTemplates();
      syncTheme();
      route();
    }, function (e) {
      if (e && e.status === 403) { noToken(); return; }
      var b = $('boot');
      clear(b);
      append(b, [h('p', { text: (e && e.message) || '连不上 Agent 程序。' }),
        h('button', { class: 'btn', type: 'button', text: '重试', on: { click: function () { location.reload(); } } })]);
    });
  }

  function noToken() {
    show($('boot'), false);
    show($('app'), false);
    show($('no-token'), true);
  }

  function loadState() {
    return api('GET', '/api/state').then(function (info) {
      S.info = info || {};
      S.workspaces = Array.isArray(S.info.workspaces) ? S.info.workspaces : [];
      if (typeof S.info.last_seq === 'number' && S.lastSeq === 0) S.lastSeq = S.info.last_seq;
      var saved = '';
      try { saved = localStorage.getItem('ca.ws') || ''; } catch (e) { saved = ''; }
      S.defaultWs = defaultWorkspaceId(S.info, S.workspaces);
      if (!S.wsId || !ws()) S.wsId = startWorkspace(S.info, S.workspaces, saved);
      // 工作模式：地址里写了就用它；空地址用上次记住的；别的不带模式的地址算 CariAi（对话的地址打开后跟着对话走）
      S.modes = normalizeModes(S.info);
      if (S.modes.length && !S.mode) {
        var hr = parseHash(location.hash), savedMode = '';
        try { savedMode = localStorage.getItem('ca.mode') || ''; } catch (e) { savedMode = ''; }
        var blank = !location.hash || location.hash === '#' || location.hash === '#/';
        S.mode = pickMode(S.modes, hr.mode || (blank || /^task\//.test(hr.path) ? savedMode : 'cariai'), savedMode);
        if (blank && S.mode === 'hspace' && history.replaceState) history.replaceState(null, '', location.pathname + location.search + '#/hspace');
      } else if (!S.modes.length) S.mode = '';
      renderModeSwitch();
      renderNav();
      renderWorkspace();
      renderModeButtons();
      return S.wsId ? loadSessions() : null;
    });
  }

  /** 取模型能力和功能状态。不阻塞启动，失败了也不提示（老后端没有这个接口时什么都不显示）。 */
  var capsReq = null;
  function loadCaps() {
    if (capsReq) return capsReq;
    capsReq = api('GET', '/api/capabilities').then(function (d) {
      S.caps = normalizeCaps(d);
    }, function (e) {
      S.caps = capsFromError(e) || S.caps;
    }).then(function () {
      capsReq = null;
      if (S.route === 'home' && S.mode === 'hspace') renderHomeKb();   // 状态行里的"现在用关键词检索"来自这里
      return S.caps;
    });
    return capsReq;
  }

  /** 某个功能现在能不能用（featureGate 的包装）。 */
  function gate(id) { return featureGate(S.caps, S.info && S.info.features, id); }

  /** 功能不可用时要说的话：后端的原话；后端没给（只从 /api/state 知道不可用）时说功能名。 */
  function gateText(g, fallbackName) {
    return g.reason || ('「' + (g.name || fallbackName) + '」现在不能用。');
  }

  function loadSessions() {
    if (!S.wsId) { S.sessions = []; renderTaskList(); return Promise.resolve(); }
    var want = S.wsId, wantMode = S.mode;
    return api('GET', apiUrl('/api/sessions', { workspace: want, mode: wantMode })).then(function (list) {
      if (want !== S.wsId || wantMode !== S.mode) return;
      S.listLoading = false;
      S.sessions = Array.isArray(list) ? list : (list && Array.isArray(list.sessions) ? list.sessions : []);
      renderTaskList();
      updateDocTitle();
    });
  }

  var listTimer = null;
  function refreshListSoon() {
    if (listTimer) return;
    listTimer = setTimeout(function () { listTimer = null; loadSessions().then(null, function () {}); }, 600);
  }

  function loadTemplates() {
    var m = S.mode;
    if (S.tplBy[m]) { S.templates = S.tplBy[m]; renderTemplates(); return; }
    api('GET', apiUrl('/api/templates', { mode: m })).then(function (list) {
      // 老后端不认 mode，会把全部模板都给回来；按每项的 mode 再筛一遍（没写 mode 的算 CariAi）
      // 新后端（每项带 source）已经按模式筛好了，没写 mode 的两个模式都有，不再筛
      var arr = (Array.isArray(list) ? list : []).filter(function (t) { return !m || !t || t.source || (str(t.mode) || 'cariai') === m; });
      S.tplBy[m] = groupTemplates(arr);
      if (S.mode === m) { S.templates = S.tplBy[m]; renderTemplates(); }
    }, function () { S.templates = []; renderTemplates(); });
  }

  /** 切到一个空间（默认空间或某个文件夹），然后去 hash 指的页面（默认是新对话）。 */
  function setWorkspace(id, hash) {
    hash = hash || '#/new';
    if (!id || id === S.wsId) { go(hash); return; }
    S.wsId = id;
    try { localStorage.setItem('ca.ws', id); } catch (e) { /* 忽略 */ }
    S.sessions = [];
    // 新对话页上已经传好的文件在原来的文件夹里，换了文件夹就用不了了
    if (S.files.home.length) {
      S.files.home = [];
      renderFiles('home');
      toast('换了文件夹，刚才添加的文件没有带过去。需要的话再添加一次。', 'info');
    }
    resetSearch(true);
    renderWorkspace();
    renderTaskList();
    loadSessions().then(null, report);
    go(hash);
  }

  /* ---------------- 路由 ----------------
   * #/new 新任务 · #/task/<id> 任务 · #/kb #/memory #/schedule #/skills #/settings · #/add 添加文件夹
   */

  function go(hash) {
    // 没写模式的地址补上当前模式（对话的地址按对话自己的模式补）
    var m = /^#\/task\/([^\/]+)/.exec(hash);
    if (S.modes.length) hash = withMode(hash, S.mode, m ? knownSessionMode(decodeURIComponent(m[1])) : '');
    if (location.hash === hash) route();
    else location.hash = hash;
  }

  /** 侧栏里有这个对话就知道它的模式；不知道时返回 ''。 */
  function knownSessionMode(sid) {
    var s = S.sessions.filter(function (x) { return x.id === sid; })[0];
    return s ? sessionMode(s) : (S.view && S.view.sid === sid && S.view.mode ? S.view.mode : '');
  }

  function taskHref(sid, mode) {
    return S.modes.length ? buildHash(mode || knownSessionMode(sid), 'task/' + encodeURIComponent(sid)) : '#/task/' + encodeURIComponent(sid);
  }

  function route() {
    closeDrawers();
    var hr = parseHash(location.hash);
    var hs = hr.path;
    var parts = hs.split('/');
    // 地址里的模式和现在的不一样（前进后退、点了另一个模式的链接）：跟着地址走。对话的地址等对话装载后再定
    if (S.modes.length) {
      var wantMode = hr.mode || (parts[0] === 'task' ? '' : 'cariai');
      if (wantMode && wantMode !== S.mode && findModeById(S.modes, wantMode)) applyMode(wantMode);
    }
    // 旧版后端没有默认空间：还是先选文件夹
    if (needsOnboard(S.workspaces, S.defaultWs) || (parts[0] === 'add' && S.defaultWs == null)) { showView('onboard'); renderOnboard(); return; }
    if (parts[0] === 'add') {
      // 有默认空间时不再有整屏的"添加文件夹"：回到新对话页，弹系统的选择窗口
      if (history.replaceState) history.replaceState(null, '', location.pathname + location.search + '#/new');
      leaveTask(); showView('home'); renderHome();
      return connectFolder();
    }
    if (parts[0] === 'task' && parts[1]) { openTask(decodeURIComponent(parts[1])); return; }
    if (PAGES[parts[0]]) { leaveTask(); showView('page'); renderPage(parts[0], parts[1] ? decodeURIComponent(parts[1]) : ''); return; }
    leaveTask();
    showView('home');
    renderHome();
  }

  function showView(name) {
    S.route = name;
    ['onboard', 'home', 'task', 'page'].forEach(function (v) { show($('view-' + v), v === name); });
    document.body.classList.toggle('no-right', name !== 'task');
    applyRight();
    applyWeb();
    closeMenus();
    var navs = document.querySelectorAll('.nav-btn[data-view]');
    var hs = parseHash(location.hash).path.split('/')[0];
    for (var i = 0; i < navs.length; i++) {
      var on = name === 'page' && navs[i].getAttribute('data-view') === hs;
      if (on) navs[i].setAttribute('aria-current', 'page'); else navs[i].removeAttribute('aria-current');
    }
    var nt = $('new-task');
    if (nt) { if (name === 'home') nt.setAttribute('aria-current', 'page'); else nt.removeAttribute('aria-current'); }
    renderTaskList();
    updateDocTitle();
  }

  function leaveTask() {
    S.sid = null;
    applyWeb();
    S.view = null;
    S.preview = null;
    S.rightSig = null;
    S.bannerSig = null;
    S.rendered = {};
    clear($('timeline'));
  }

  /* ---------------- 网页面板（docs/操控电脑.md 第 9 节） ----------------
   * 右边显示这个对话的标签页：画面是服务端推来的 MJPEG（<img> 直接放），点、滚、打字、地址栏都发给 /api/browser/*。
   * Agent 开始操作网页时自动打开（这个对话里你关过就不再自动开）；窄窗口时盖在上面，不自动开。
   */

  var WEB = { open: false, sid: null, state: null, closedFor: {}, acting: {}, avail: {}, n: 0, sizeSent: '',
              chain: Promise.resolve(), wheel: null, moveAt: 0, errAt: 0 };
  var BLANK_GIF = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';

  function webHere() { return S.route === 'task' && !!S.sid; }

  function setWebOpen(open, byUser) {
    WEB.open = !!open;
    if (S.sid && byUser) {
      if (open) delete WEB.closedFor[S.sid]; else WEB.closedFor[S.sid] = true;
    }
    applyWeb();
    if (open && byUser && isWide() === false) closeDrawers();
  }

  function applyWeb() {
    var on = WEB.open && webHere();
    document.body.classList.toggle('web-open', on);
    if (on && WEB.sid !== S.sid) webConnect(S.sid);
    else if (!on && WEB.sid) webDisconnect();
    renderWebToggle();
  }

  function renderWebToggle() {
    var b = $('btn-web-toggle');
    if (!b) return;
    var on = WEB.open && webHere();
    b.hidden = !(on || (S.sid && WEB.avail[S.sid]));
    b.setAttribute('aria-expanded', on ? 'true' : 'false');
    b.title = on ? '关闭网页面板' : '在右边看 Agent 操作的网页，也可以自己操作';
  }

  function webStreamUrl(sid) { return apiUrl('/api/browser/view', { session_id: sid, t: S.token, n: ++WEB.n }); }

  function webConnect(sid) {
    webDisconnect();
    WEB.sid = sid;
    var img = $('wp-img');
    if (img) {
      img.onerror = function () {
        // 程序重启、连接断了：过一会儿重连（面板还开着、还是这个对话才连）
        setTimeout(function () { if (WEB.sid === sid && WEB.open && webHere()) img.src = webStreamUrl(sid); }, 3000);
      };
      img.src = webStreamUrl(sid);
    }
    WEB.sizeSent = '';
    webSize();
    webFetchState(sid);
    renderWeb();
  }

  /** 告诉服务端面板画面区域有多大：网页按这个比例排版，画面正好铺满（宽至少 1024，再窄就缩小显示）。 */
  function webSize() {
    var stage = $('wp-stage');
    if (!WEB.sid || !stage || !stage.clientWidth || !stage.clientHeight) return;
    var key = WEB.sid + ':' + Math.round(stage.clientWidth / 8) + 'x' + Math.round(stage.clientHeight / 8);
    if (key === WEB.sizeSent) return;
    WEB.sizeSent = key;
    api('POST', '/api/browser/size', { session_id: WEB.sid, width: stage.clientWidth, height: stage.clientHeight }).then(null, function () { WEB.sizeSent = ''; });
  }

  function webDisconnect() {
    var img = $('wp-img');
    if (img) { img.onerror = null; img.src = BLANK_GIF; }     // 换掉 src，浏览器就会断开画面的连接
    WEB.sid = null;
  }

  function webFetchState(sid) {
    return api('GET', '/api/browser/state?session_id=' + encodeURIComponent(sid)).then(function (d) {
      d = d || {};
      WEB.avail[sid] = d.available !== false;
      if (WEB.sid === sid) { WEB.state = d; renderWeb(); }
      if (S.sid === sid) renderWebToggle();
      return d;
    }, function () { return null; });
  }

  /** 打开了另一个对话：那个对话有开着的网页就接着显示；没有就收起面板（你手动打开的除外）。 */
  function webOnTask(sid) {
    WEB.state = null;
    webFetchState(sid).then(function (d) {
      if (S.sid !== sid || !WEB.open) return;
      if (!d || !d.open) { WEB.open = false; applyWeb(); }
    });
    if (WEB.open) applyWeb();
  }

  function onBrowserView(sid, p) {
    if (!sid) return;
    WEB.avail[sid] = true;
    if (sid === WEB.sid) {
      WEB.state = Object.assign({}, WEB.state || {}, p);
      renderWeb();
    }
    if (sid === S.sid) {
      if (p.open && !WEB.open && !WEB.closedFor[sid] && isWide()) setWebOpen(true, false);
      else renderWebToggle();
      if (p.note) toast(p.note, 'info');
    }
  }

  function webOnEvent(ev) {
    var p = ev.payload || {};
    var tool = str(p.tool);
    var sid = S.sid;
    if (ev.type === 'tool_started' && /^browser_/.test(tool)) {
      WEB.acting[sid] = true;
      WEB.avail[sid] = true;
      if (!WEB.open && !WEB.closedFor[sid] && isWide()) setWebOpen(true, false);
      else renderWebToggle();
      renderWeb();
    } else if ((ev.type === 'tool_result' || ev.type === 'approval_requested') && /^browser_/.test(tool)) {
      WEB.acting[sid] = false;
      renderWeb();
      if (WEB.sid === sid) webFetchState(sid);
    } else if (ev.type === 'status') {
      var st = p.status;
      if (st !== 'running') { WEB.acting[sid] = false; renderWeb(); }
      // Agent 停下来等你回答（例如请你登录、过人机验证）：网页开着就把面板打开
      if (st === 'waiting_input' && WEB.state && WEB.state.open && !WEB.open && !WEB.closedFor[sid] && isWide()) setWebOpen(true, false);
    }
  }

  function renderWeb() {
    var st = WEB.state;
    var screen = $('wp-screen'), empty = $('wp-empty'), foot = $('wp-foot'), addr = $('wp-addr');
    if (!screen) return;
    var open = !!(st && st.open);
    screen.hidden = !open;
    empty.hidden = open;
    if (!open) {
      clear(empty);
      if (st && st.available === false) {
        empty.appendChild(h('p', { class: 'wp-empty-title', text: '「操作网页」现在用不了' }));
        if (st.reason) empty.appendChild(h('p', { text: st.reason }));
      } else {
        empty.appendChild(h('p', { class: 'wp-empty-title', text: '这个对话还没有打开网页' }));
        empty.appendChild(h('p', { text: 'Agent 打开网页后会显示在这里，你可以看着它操作，也可以直接在画面上点、滚动、打字。' }));
        empty.appendChild(h('p', { text: '也可以在上面输入网址或要搜的词，自己先打开，比如先登录要用的系统。' }));
      }
    }
    if (addr && document.activeElement !== addr) addr.value = open ? str(st.url) : '';
    var acting = !!WEB.acting[WEB.sid];
    foot.classList.toggle('is-agent', acting && open);
    foot.textContent = acting && open ? 'Agent 正在操作这个网页'
      : !open ? '' : st.view === 'window' ? '浏览器开在单独的窗口里，这里同步显示。' : (str(st.title) || '点画面就能操作；要打字，先点一下输入框。');
    renderWebDialog(open ? st.dialog : null);
  }

  function renderWebDialog(d) {
    var box = $('wp-dialog');
    if (!box) return;
    clear(box);
    box.hidden = !d;
    if (!d) return;
    var kinds = { alert: '网页提示', confirm: '网页要你确认', prompt: '网页要你填写' };
    box.setAttribute('aria-label', kinds[d.type] || '网页弹框');
    box.appendChild(h('p', { class: 'wp-dialog-kind', text: kinds[d.type] || '网页弹框' }));
    box.appendChild(h('p', { class: 'wp-dialog-msg', text: str(d.message) }));
    var inp = d.type === 'prompt' ? h('input', { type: 'text', value: str(d['default']), 'aria-label': '要填的内容' }) : null;
    if (inp) box.appendChild(inp);
    function answer(accept) {
      return api('POST', '/api/browser/dialog', { session_id: WEB.sid, accept: accept, text: inp ? inp.value : null }).then(function () {
        if (WEB.state) WEB.state.dialog = null;
        renderWeb();
        webFetchState(WEB.sid);
      });
    }
    var acts = h('div', { class: 'wp-dialog-acts' });
    if (d.type !== 'alert') acts.appendChild(h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '取消', on: { click: guard(function () { return answer(false); }) } }));
    var ok = h('button', { class: 'btn btn-primary btn-sm', type: 'button', text: '确定', on: { click: guard(function () { return answer(true); }) } });
    acts.appendChild(ok);
    box.appendChild(acts);
    setTimeout(function () { (inp || ok).focus(); }, 0);
  }

  /** 发一个操作：按顺序一个一个发（点了再打字，顺序不能乱）；出错只提示一次。 */
  function webSend(payload) {
    var sid = WEB.sid;
    if (!sid) return WEB.chain;
    WEB.chain = WEB.chain.then(function () {
      return api('POST', '/api/browser/input', Object.assign({ session_id: sid }, payload));
    }).then(null, function (e) {
      if (Date.now() - WEB.errAt > 4000) { WEB.errAt = Date.now(); toast((e && e.message) || '网页没响应', 'warn'); }
    });
    return WEB.chain;
  }

  function webNav(action, url) {
    var sid = WEB.sid;
    if (!sid) return Promise.resolve();
    WEB.acting[sid] = false;
    return api('POST', '/api/browser/nav', { session_id: sid, action: action, url: url || '' }).then(function () {
      return webFetchState(sid);
    }, function (e) { toast((e && e.message) || '没打开', 'warn'); });
  }

  function webPos(e) {
    var img = $('wp-img');
    var r = img.getBoundingClientRect();
    if (!r.width || !r.height) return null;
    var cl = function (v) { return Math.max(0, Math.min(1, v)); };
    var vw = (WEB.state && WEB.state.w) || 1280;
    return { x: cl((e.clientX - r.left) / r.width), y: cl((e.clientY - r.top) / r.height), scale: vw / r.width };
  }

  var WEB_KEYS = { Enter: 1, Backspace: 1, Delete: 1, Tab: 1, Escape: 1, ArrowUp: 1, ArrowDown: 1, ArrowLeft: 1, ArrowRight: 1,
                   Home: 1, End: 1, PageUp: 1, PageDown: 1 };

  /** 按键 → 发给网页的键名；不发的返回 ''；交给输入框自己处理（打字、粘贴、输入法）的返回 null。 */
  function webKeyName(e) {
    if (e.isComposing || e.keyCode === 229) return null;
    if (e.metaKey || e.ctrlKey) {
      var k = String(e.key || '').toLowerCase();
      if (k === 'a') return 'ControlOrMeta+a';
      if (k === 'z') return e.shiftKey ? 'ControlOrMeta+Shift+z' : 'ControlOrMeta+z';
      if (k === 'v') return null;
      return '';
    }
    if (WEB_KEYS[e.key]) {
      if (e.shiftKey && (e.key === 'Tab' || e.key === 'Enter')) return 'Shift+' + e.key;
      return e.key;
    }
    return null;
  }

  function webFlushKeys() {
    var keys = $('wp-keys');
    var v = keys.value;
    if (!v) return;
    keys.value = '';
    for (var i = 0; i < v.length; i += 2000) webSend({ kind: 'text', text: v.slice(i, i + 2000) });
  }

  function bindWeb() {
    var screen = $('wp-screen'), img = $('wp-img'), keys = $('wp-keys');
    if (!screen) return;
    [['wp-back', 'back', '后退'], ['wp-fwd', 'chevron', '前进'], ['wp-reload', 'reload', '刷新'], ['wp-close', 'x', '关闭']].forEach(function (b) {
      var el = $(b[0]);
      if (el && !el.firstChild) el.appendChild(icon(b[1], 16));
    });
    on('btn-web-toggle', 'click', function () { setWebOpen(!(WEB.open && webHere()), true); });
    on('wp-close', 'click', function () { setWebOpen(false, true); });
    on('wp-back', 'click', function () { return webNav('back'); });
    on('wp-fwd', 'click', function () { return webNav('forward'); });
    on('wp-reload', 'click', function () { return webNav('reload'); });
    on('wp-addr-form', 'submit', function (e) {
      e.preventDefault();
      var inp = $('wp-addr');
      var v = String(inp.value || '').trim();
      if (!v || !S.sid) return;
      if (WEB.sid !== S.sid) { WEB.open = true; applyWeb(); }
      inp.blur();
      return webNav('go', v);
    });
    on('wp-addr', 'keydown', function (e) {
      if (e.key === 'Escape') { e.preventDefault(); e.target.value = WEB.state && WEB.state.open ? str(WEB.state.url) : ''; e.target.blur(); }
    });
    img.addEventListener('click', function (e) {
      e.preventDefault();
      var p = webPos(e);
      if (!p) return;
      if (e.detail === 1) webSend({ kind: 'click', x: p.x, y: p.y });
      else if (e.detail === 2) webSend({ kind: 'dblclick', x: p.x, y: p.y });
      keys.focus({ preventScroll: true });
    });
    img.addEventListener('mousemove', function (e) {
      if (Date.now() - WEB.moveAt < 150) return;       // 只为了让悬停菜单出得来，不用每一下都发
      WEB.moveAt = Date.now();
      var p = webPos(e);
      if (p) webSend({ kind: 'move', x: p.x, y: p.y });
    });
    screen.addEventListener('wheel', function (e) {
      var p = webPos(e);
      if (!p) return;
      e.preventDefault();
      var k = e.deltaMode === 1 ? 32 : e.deltaMode === 2 ? 600 : 1;
      var w = WEB.wheel || (WEB.wheel = { dx: 0, dy: 0 });
      w.dx += e.deltaX * k * p.scale;
      w.dy += e.deltaY * k * p.scale;
      w.x = p.x; w.y = p.y;
      if (w.timer) return;
      w.timer = setTimeout(function () {
        WEB.wheel = null;
        var dx = Math.max(-5000, Math.min(5000, w.dx)), dy = Math.max(-5000, Math.min(5000, w.dy));
        if (dx || dy) webSend({ kind: 'wheel', x: w.x, y: w.y, dx: dx, dy: dy });
      }, 60);
    }, { passive: false });
    keys.addEventListener('focus', function () { screen.classList.add('is-focus'); });
    keys.addEventListener('blur', function () { webFlushKeys(); screen.classList.remove('is-focus'); });
    keys.addEventListener('keydown', function (e) {
      var name = webKeyName(e);
      if (name === null) return;
      e.preventDefault();
      if (!name) return;
      webFlushKeys();
      webSend({ kind: 'key', key: name });
    });
    keys.addEventListener('input', function (e) { if (!e.isComposing) webFlushKeys(); });
    keys.addEventListener('compositionend', function () { setTimeout(webFlushKeys, 0); });
    if (typeof ResizeObserver !== 'undefined') {
      var sizeTimer = null;
      new ResizeObserver(function () {
        clearTimeout(sizeTimer);
        sizeTimer = setTimeout(webSize, 250);
      }).observe($('wp-stage'));
    }
  }

  /* ---------------- 右栏开关 ----------------
   * 宽屏：右栏默认收起，点顶部"计划与成品"打开（记住这个选择）；窄屏：右栏是抽屉。
   */

  function isWide() { return (window.innerWidth || 1400) > 1100; }

  function rightShown() {
    if (S.route !== 'task') return false;
    return isWide() ? !!S.rightOpen : document.body.classList.contains('show-right');
  }

  function applyRight() {
    document.body.classList.toggle('right-closed', !S.rightOpen);
    var rt = $('btn-right-toggle');
    if (rt) rt.setAttribute('aria-expanded', rightShown() ? 'true' : 'false');
  }

  function setRightOpen(open) {
    S.rightOpen = !!open;
    try { localStorage.setItem('ca.right', S.rightOpen ? '1' : '0'); } catch (e) { /* 忽略 */ }
    applyRight();
  }

  function toggleRight() {
    if (isWide()) {
      setRightOpen(!S.rightOpen);
      if (S.rightOpen) { S.rightSig = null; renderRight(); }
      else if (S.preview) closePreview();
    } else {
      toggleDrawer('right');
    }
    if (S.view) renderTask();
  }

  /* ---------------- 左栏 ---------------- */

  function renderWorkspace() {
    renderFolders();
    renderFolderTags();
  }

  var SVG_NS = 'http://www.w3.org/2000/svg';

  /** 线条图标：16×16、线宽 1.5、圆头，颜色跟随文字。每个图标是几段 path。 */
  var ICONS = {
    plus: ['M8 3.25v9.5M3.25 8h9.5'],
    search: ['M7 2.75a4.25 4.25 0 1 0 0 8.5 4.25 4.25 0 0 0 0-8.5z', 'M10.1 10.1l3.15 3.15'],
    sidebar: ['M2.75 3.25h10.5v9.5H2.75z', 'M6.25 3.25v9.5'],
    menu: ['M2.75 4.5h10.5M2.75 8h10.5M2.75 11.5h10.5'],
    book: ['M3.25 2.75h7.5l2 2v8.5h-9.5z', 'M5.75 6.25h4.5M5.75 8.75h4.5M5.75 11.25h2.5'],
    lock: ['M4.25 7.25h7.5v6h-7.5z', 'M5.75 7.25V5.5a2.25 2.25 0 0 1 4.5 0v1.75'],
    tools: ['M2.75 4.25h10.5M2.75 8h10.5M2.75 11.75h6.5'],
    memory: ['M4.25 2.75h7.5v10.5L8 10.75l-3.75 2.5z'],
    clock: ['M8 2.25a5.75 5.75 0 1 0 0 11.5 5.75 5.75 0 0 0 0-11.5z', 'M8 4.75V8l2.25 1.5'],
    skill: ['M9 1.75 3.75 9h4.25l-1 5.25L12.25 7H8z'],
    settings: ['M2.75 4.5h6.5M11.75 4.5h1.5M2.75 11.5h1.5M6.75 11.5h6.5', 'M10.5 3a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3zM5.5 10a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3z'],
    command: ['M5.5 5.5h5v5h-5z', 'M5.5 5.5V4.25a1.5 1.5 0 1 0-1.5 1.25zM10.5 5.5V4.25a1.5 1.5 0 1 1 1.5 1.25zM5.5 10.5v1.25a1.5 1.5 0 1 1-1.5-1.25zM10.5 10.5v1.25a1.5 1.5 0 1 0 1.5-1.25z'],
    chat: ['M2.75 3.75h10.5v7h-6l-3 2.5v-2.5h-1.5z'],
    more: ['M3.5 8h.01M8 8h.01M12.5 8h.01'],
    chevron: ['M6.25 4l4 4-4 4'],
    down: ['M4 6.25l4 4 4-4'],
    check: ['M3.5 8.25l3 3 6-6.5'],
    x: ['M4.25 4.25l7.5 7.5M11.75 4.25l-7.5 7.5'],
    copy: ['M5.75 5.75h7.5v7.5h-7.5z', 'M3.25 10.25v-7h7'],
    download: ['M8 2.75v7.5M4.75 7.25 8 10.5l3.25-3.25M3 13.25h10'],
    file: ['M4 1.75h5.25L12.5 5v9.25h-8.5z', 'M9 1.75V5.25h3.5'],
    folder: ['M2 4.25c0-.69.56-1.25 1.25-1.25h3l1.5 1.75h5c.69 0 1.25.56 1.25 1.25v5.75c0 .69-.56 1.25-1.25 1.25h-9.5C2.56 13 2 12.44 2 11.75z'],
    alert: ['M8 2.25a5.75 5.75 0 1 0 0 11.5 5.75 5.75 0 0 0 0-11.5z', 'M8 5v3.5M8 10.75h.01'],
    info: ['M8 2.25a5.75 5.75 0 1 0 0 11.5 5.75 5.75 0 0 0 0-11.5z', 'M8 7.25v3.5M8 5.25h.01'],
    spark: ['M8 2.5v3M8 10.5v3M2.5 8h3M10.5 8h3M4.4 4.4l1.6 1.6M10 10l1.6 1.6M4.4 11.6 6 10M10 6l1.6-1.6'],
    undo: ['M5.5 4.25 2.75 7l2.75 2.75', 'M2.75 7h6.5a3.5 3.5 0 0 1 0 7H7.5'],
    summary: ['M3 3.75h10M3 6.75h10M3 9.75h6.5M3 12.75h4'],
    globe: ['M8 2.25a5.75 5.75 0 1 0 0 11.5 5.75 5.75 0 0 0 0-11.5z', 'M2.25 8h11.5', 'M8 2.25c1.6 1.6 2.4 3.5 2.4 5.75S9.6 12.15 8 13.75C6.4 12.15 5.6 10.25 5.6 8S6.4 3.85 8 2.25z'],
    monitor: ['M2.25 3h11.5v7.5H2.25z', 'M6 13.25h4M8 10.5v2.75'],
    back: ['M9.75 4l-4 4 4 4'],
    reload: ['M12.75 8a4.75 4.75 0 1 1-1.4-3.36', 'M12.75 2.75v2.75H10'],
    thumbUp: ['M5 7.25v6.5H2.75v-6.5z', 'M5 7.25 7.6 2.75c.85 0 1.4.6 1.4 1.4v2.1h3.05c.8 0 1.4.75 1.2 1.5l-1.05 4.6c-.15.6-.65 1-1.25 1H5'],
    thumbDown: ['M5 8.75v-6.5H2.75v6.5z', 'M5 8.75l2.6 4.5c.85 0 1.4-.6 1.4-1.4v-2.1h3.05c.8 0 1.4-.75 1.2-1.5l-1.05-4.6c-.15-.6-.65-1-1.25-1H5']
  };

  /** 建一个图标。name 是 ICONS 里的名字，也可以直接给一段 path。 */
  function icon(name, size) {
    var svg = document.createElementNS(SVG_NS, 'svg');
    var n = String(size || 16);
    svg.setAttribute('viewBox', '0 0 16 16');
    svg.setAttribute('width', n);
    svg.setAttribute('height', n);
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    svg.setAttribute('class', 'ic');
    (ICONS[name] || [name]).forEach(function (d) {
      var p = document.createElementNS(SVG_NS, 'path');
      p.setAttribute('d', d);
      p.setAttribute('fill', 'none');
      p.setAttribute('stroke', 'currentColor');
      p.setAttribute('stroke-width', '1.5');
      p.setAttribute('stroke-linecap', 'round');
      p.setAttribute('stroke-linejoin', 'round');
      svg.appendChild(p);
    });
    return svg;
  }
  var IC_FILE = 'file';
  var IC_FOLDER = 'folder';

  /** index.html 里写了 data-icon 的元素：在最前面放上对应的图标（只放一次）。 */
  function paintIcons(root) {
    var els = (root || document).querySelectorAll('[data-icon]');
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (el.querySelector(':scope > svg.ic')) continue;
      el.insertBefore(icon(el.getAttribute('data-icon'), 16), el.firstChild);
    }
  }

  /** 在默认空间之外的某个文件夹里吗。 */
  function inFolder() {
    var w = ws();
    return !!w && (S.defaultWs == null || w.id !== S.defaultWs);
  }

  /** 侧栏的"文件夹"小节和对话列表的标题。 */
  function renderFolders() {
    var box = $('folder-list');
    if (box) {
      clear(box);
      folderList(S.workspaces, S.defaultWs, S.wsId).forEach(function (f) {
        // 默认空间存在时，再点一次当前文件夹就回到默认空间
        var back = f.current && S.defaultWs != null;
        box.appendChild(h('div', { class: 'folder-item' + (f.current ? ' is-current' : '') },
          h('button', { class: 'folder-open', type: 'button', 'aria-current': f.current ? 'true' : null,
            title: back ? '再点一次回到全部对话。位置：' + f.path : f.path,
            on: { click: function () { setWorkspace(back ? S.defaultWs : f.id); } } },
            icon(IC_FOLDER), h('span', { class: 'folder-name', text: f.name })),
          h('button', { class: 'folder-more', type: 'button', 'aria-haspopup': 'menu', 'aria-expanded': 'false',
            'aria-label': '「' + f.name + '」的更多操作', title: '改名、移除',
            on: { click: function (e) { openFolderMenu(e.currentTarget, f.id); } } }, '⋯')));
      });
      box.appendChild(h('button', { class: 'folder-add', type: 'button', disabled: S.picking, 'aria-busy': S.picking ? 'true' : null,
        on: { click: function () { return connectFolder(); } } },
        h('span', { class: 'folder-add-mark', 'aria-hidden': 'true', text: '+' }),
        h('span', { text: S.picking ? '正在等你选择文件夹…' : '添加文件夹…' })));
    }
    var w = ws(), here = inFolder() && S.defaultWs != null;
    setText($('list-caption'), here ? '「' + w.name + '」里的对话' : '对话');
    show($('list-all'), here);
    // 不在文件夹里时，列表自己按时间分了组，上面不再写"对话"
    var lc = $('list-caption');
    if (lc && lc.parentNode) show(lc.parentNode, here);
  }

  /** 输入框上方的"在「某某」文件夹里"。在默认空间时什么都不显示。 */
  function renderFolderTags() {
    var w = ws(), on = inFolder();
    ['home', 'composer'].forEach(function (which) {
      var el = $(which + '-folder');
      if (!el) return;
      clear(el);
      el.hidden = !on;
      if (!on) return;
      el.title = str(w.path);
      append(el, [icon(IC_FOLDER, 13), h('span', { class: 'ft-text' }, '在「', h('span', { class: 'strong', text: w.name }), '」文件夹里')]);
      // 新对话页可以退回默认空间；已经开始的对话属于这个文件夹，不能换
      if (which === 'home' && S.defaultWs != null) {
        el.appendChild(h('button', { class: 'ft-x', type: 'button', 'aria-label': '不用这个文件夹', title: '不用这个文件夹',
          text: '×', on: { click: function () { setWorkspace(S.defaultWs); } } }));
      }
    });
  }

  /* ---------------- 弹出菜单（+、⋯、文件夹操作、批准方式） ---------------- */

  var MENUS = ['mode-menu', 'plus-menu', 'folder-menu', 'task-menu', 'scope-menu', 'doc-pop'];
  var pop = { btn: null };

  /** 打开一个弹出菜单。再点同一个按钮就关上。build(menu) 往里放菜单项。 */
  function openPop(id, btn, build) {
    var menu = $(id);
    if (!menu || !btn) return;
    var same = !menu.hidden && pop.btn === btn;
    closeMenus();
    if (same) return;
    clear(menu);
    build(menu);
    menu.hidden = false;
    pop.btn = btn;
    btn.setAttribute('aria-expanded', 'true');
    placeMenu(menu, btn);
    var first = menu.querySelector('.menu-item:not([disabled])');
    if (first) first.focus();
  }

  /** 菜单贴着按钮放：下面放得下放下面，否则放上面；不超出窗口。 */
  function placeMenu(menu, btn) {
    var r = btn.getBoundingClientRect();
    var mh = menu.offsetHeight || 200, mw = menu.offsetWidth || 260;
    var vh = window.innerHeight || 800, vw = window.innerWidth || 1200;
    var top = r.bottom + 4;
    if (top + mh > vh - 8) top = Math.max(8, r.top - mh - 4);
    var left = Math.min(Math.max(8, r.left), vw - mw - 8);
    menu.style.top = top + 'px';
    menu.style.left = Math.max(8, left) + 'px';
  }

  function menuItem(label, desc, run, danger) {
    return h('button', { class: 'menu-item' + (danger ? ' is-danger' : ''), type: 'button', role: 'menuitem',
      on: { click: function () { closeMenus(true); return run(); } } },
      h('span', { class: 'mi-label', text: label }), desc ? h('span', { class: 'mi-desc', text: desc }) : null);
  }

  /** 关上所有弹出菜单。focusBack 为真时焦点回到打开菜单的按钮。返回是否关了什么。 */
  function closeMenus(focusBack) {
    var any = false;
    MENUS.forEach(function (id) { var m = $(id); if (m && !m.hidden) { m.hidden = true; any = true; } });
    var b = pop.btn;
    pop.btn = null;
    if (b) {
      if (b.hasAttribute('aria-expanded')) b.setAttribute('aria-expanded', 'false');
      if (focusBack && any && document.contains(b)) { try { b.focus(); } catch (e) { /* 忽略 */ } }
    }
    return any;
  }

  function openFolderMenu(btn, id) {
    var w = S.workspaces.filter(function (x) { return x.id === id; })[0];
    if (!w) return;
    openPop('folder-menu', btn, function (m) {
      m.appendChild(h('div', { class: 'menu-caption mono', text: w.path }));
      m.appendChild(menuItem('在这里开新对话', '', function () { setWorkspace(w.id); }));
      m.appendChild(menuItem('改名…', '', function () { return renameFolder(w); }));
      m.appendChild(menuItem('说明和网络设置…', '告诉 Agent 这个文件夹里是什么', function () { setWorkspace(w.id, '#/settings'); }));
      if (!w.is_default) {
        m.appendChild(h('div', { class: 'menu-sep' }));
        m.appendChild(menuItem('不再使用这个文件夹', '只从列表里去掉，文件不会动', function () { return removeFolder(w); }, true));
      }
    });
  }

  function renameFolder(w) {
    var name = window.prompt('给「' + w.name + '」换个名字：', w.name || '');
    if (name == null) return;
    name = name.trim();
    if (!name || name === w.name) return;
    return api('PATCH', '/api/workspaces/' + encodeURIComponent(w.id), { name: name.slice(0, 60) }).then(function (nw) {
      for (var i = 0; i < S.workspaces.length; i++) {
        if (S.workspaces[i].id === w.id) S.workspaces[i] = nw && nw.id ? nw : Object.assign({}, S.workspaces[i], { name: name });
      }
      renderWorkspace();
      toast('已改名。', 'info');
    });
  }

  function removeFolder(w) {
    if (!window.confirm('不再使用「' + w.name + '」？只是从列表里去掉，文件夹里的文件不会动。')) return;
    return api('DELETE', '/api/workspaces/' + encodeURIComponent(w.id)).then(function () {
      S.workspaces = S.workspaces.filter(function (x) { return x.id !== w.id; });
      var wasCurrent = S.wsId === w.id;
      if (wasCurrent) { S.wsId = null; resetSearch(true); }
      return loadState().then(function () {
        if (wasCurrent) go(S.workspaces.length ? '#/new' : '#/add');
        toast('已从列表里去掉「' + w.name + '」。', 'info');
      });
    });
  }

  /* ---------------- 连接文件夹 ----------------
   * 先弹系统自带的"选择文件夹"窗口（/api/pick_folder）；这台机器弹不了、或者程序是旧版本时，
   * 退回手工填名称和路径的对话框。用户点了取消就什么都不做。
   */

  function connectFolder() {
    if (S.picking) { toast('选择文件夹的窗口已经开着了。没看到的话，它可能在浏览器后面。', 'info'); return; }
    if (S.noPicker) { openFolderDialog(); return; }
    S.picking = true;
    renderFolders();          // "添加文件夹…"变成"请在弹出的窗口里选择…"，不能再点
    // 这个请求最长会等 120 秒（等用户在系统窗口里选）
    var note = pickNote('请在弹出的窗口里选择文件夹…（没看到的话，它可能在浏览器后面）');
    function done() { S.picking = false; note(); renderFolders(); }
    return api('POST', '/api/pick_folder', {}).then(function (d) {
      done();
      if (!d || d.supported === false) { S.noPicker = true; openFolderDialog(); return; }
      if (!d.path) return;                          // 用户点了取消，或者等太久窗口关了
      return addFolder(str(d.name), str(d.path));
    }, function (e) {
      done();
      if (e && e.status === 409) { toast('选择文件夹的窗口已经开着了。没看到的话，它可能在浏览器后面。', 'info'); return; }
      if (e && (e.missing || e.status === 404)) { S.noPicker = true; openFolderDialog(); return; }
      throw e;
    });
  }

  /** 建好文件夹并切过去，停在新对话页。已经连过的同一个文件夹直接切过去，不重复添加。 */
  function addFolder(name, path) {
    var have = S.workspaces.filter(function (w) { return samePath(w.path, path); })[0];
    if (have) {
      setWorkspace(have.id, '#/new');
      toast(have.id === S.defaultWs ? '这是默认的文件夹，已经回到全部对话。' : '「' + have.name + '」已经连过了，已切换过去。', 'info');
      return Promise.resolve(have);
    }
    var n = str(name).trim() || fileName(str(path).replace(/[\\\/]+$/, '')) || '文件夹';
    return api('POST', '/api/workspaces', { name: n.slice(0, 60), path: path }).then(function (w) {
      w = w && w.workspace ? w.workspace : w;
      if (!w || !w.id) throw new Error('文件夹没有连上，请再试一次。');
      S.workspaces.push(w);
      renderWorkspace();
      setWorkspace(w.id, '#/new');
      return w;
    });
  }

  var dialog = { returnFocus: null };

  function openFolderDialog() {
    var d = $('folder-dialog');
    if (!d) return;
    closeMenus();
    if (d.hidden) dialog.returnFocus = document.activeElement;
    var err = $('folder-error');
    if (err) err.hidden = true;
    d.hidden = false;
    syncScrim();
    var path = $('folder-path');
    if (path) setTimeout(function () { try { path.focus(); } catch (e) { /* 忽略 */ } }, 0);
  }

  function closeFolderDialog() {
    var d = $('folder-dialog');
    if (!d || d.hidden) return false;
    d.hidden = true;
    syncScrim();
    var back = dialog.returnFocus;
    dialog.returnFocus = null;
    if (back && back.focus && document.contains(back)) { try { back.focus(); } catch (e) { /* 忽略 */ } }
    return true;
  }

  function submitFolderForm(e) {
    if (e) e.preventDefault();
    var name = $('folder-name'), path = $('folder-path'), err = $('folder-error'), btn = $('folder-submit');
    var n = name ? name.value.trim() : '', p = path ? path.value.trim() : '';
    if (!p) { if (err) { err.textContent = '请填写文件夹路径。'; err.hidden = false; } if (path) path.focus(); return; }
    if (btn) btn.disabled = true;
    return addFolder(n, p).then(function () {
      if (btn) btn.disabled = false;
      if (name) name.value = '';
      if (path) path.value = '';
      dialog.returnFocus = null;
      closeFolderDialog();
      var input = $('home-input');
      if (input && S.route === 'home') input.focus();
    }, function (ex) {
      if (btn) btn.disabled = false;
      if (err) { err.textContent = (ex && ex.message) || '没有连上。'; err.hidden = false; }
    });
  }

  /** 对话框里按 Tab 不跑出去。 */
  function trapTab(e, box) {
    if (e.key !== 'Tab') return;
    var f = Array.prototype.filter.call(box.querySelectorAll('button, input, select, textarea, a[href]'), function (x) { return !x.disabled && !x.hidden; });
    if (!f.length) return;
    var first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }

  /** 遮罩：抽屉、命令面板、对话框任何一个开着就显示。 */
  function syncScrim() {
    var b = document.body.classList;
    var on = b.contains('show-left') || b.contains('show-right') ||
      !!($('palette') && !$('palette').hidden) || !!($('folder-dialog') && !$('folder-dialog').hidden);
    show($('scrim'), on);
  }

  function renderTaskList() {
    var box = $('task-list');
    if (!box) return;
    clear(box);
    if (!S.wsId) return;
    if (S.search.q) { renderSearchResults(box); return; }
    box.setAttribute('aria-label', '对话列表');
    if (S.listLoading) return;      // 刚换了模式，列表还在路上：先空着，免得闪一下"还没有对话"
    var list = sessionsForMode(S.sessions, S.mode);
    if (!list.length) {
      box.appendChild(h('p', { class: 'list-empty', text: inFolder() && S.defaultWs != null ? '这个文件夹里还没有对话。'
        : S.mode === 'hspace' ? '还没有 ' + modeName('hspace') + ' 的对话。' : '还没有对话。' }));
      return;
    }
    var now = Date.now();
    groupSessions(list, now).forEach(function (g) {
      box.appendChild(h('div', { class: 'list-caption list-group', text: g.label }));
      g.items.forEach(function (s) { box.appendChild(taskItem(s, now)); });
    });
  }

  /** 侧栏的一项：标题；右边平时是时间，等你处理、正在做、出错时换成一个小圆点（悬停看文字）。 */
  function taskItem(s, now) {
    var st = s.status || 'idle';
    var cur = s.id === S.sid;
    var reason = s.meta && s.meta.reason ? s.meta.reason : '';
    var stText = statusText(st) + (st === 'paused' && reason === '程序重启' ? '（中断）' : '');
    var need = needsAttention(st), mark = need ? 'need' : isActive(st) ? 'running' : st === 'failed' ? 'failed' : '';
    var title = s.title || '未命名对话';
    return h('a', {
      class: 'task-item' + (need ? ' needs-you' : '') + (cur ? ' is-current' : ''),
      href: taskHref(s.id, S.modes.length ? sessionMode(s) : ''),
      'aria-current': cur ? 'page' : null,
      title: title + (mark ? '（' + stText + '）' : '')
    },
      h('span', { class: 'ti-title', text: title }),
      mark ? h('span', { class: 'ti-dot is-' + mark }, h('span', { class: 'sr-only', text: stText }))
        : h('span', { class: 'ti-time', text: listTime(s.updated, now) }));
  }

  /* ---------------- 搜索以前的对话 ---------------- */

  function onSearchInput() {
    var input = $('task-search');
    var q = searchQuery(input ? input.value : '');
    var st = S.search;
    if (st.timer) { clearTimeout(st.timer); st.timer = null; }
    if (!q) { resetSearch(false); renderTaskList(); return; }
    if (q === st.q && st.ws === S.wsId && (st.hits || st.loading)) return;
    st.q = q;
    st.loading = true;
    st.error = '';
    st.seq++;            // 让还在路上的旧请求作废
    renderTaskList();
    st.timer = setTimeout(function () { st.timer = null; runSearch(); }, 300);
  }

  function runSearch() {
    var st = S.search;
    if (!st.q || !S.wsId) return;
    var my = ++st.seq, wsId = S.wsId;
    st.loading = true;
    api('GET', apiUrl('/api/search', { q: st.q, workspace: wsId, mode: S.mode })).then(function (d) {
      if (my !== st.seq) return;
      var r = normalizeHits(d);
      st.hits = r.hits;
      st.note = r.note;
      st.loading = false;
      st.ws = wsId;
      renderTaskList();
    }, function (e) {
      if (my !== st.seq) return;
      st.hits = [];
      st.note = '';
      st.loading = false;
      st.ws = wsId;
      st.error = (e && e.message) || '没有查成。';
      renderTaskList();
    });
  }

  function resetSearch(clearInput) {
    var st = S.search;
    if (st.timer) { clearTimeout(st.timer); st.timer = null; }
    st.seq++;
    st.q = ''; st.hits = null; st.note = ''; st.error = ''; st.loading = false; st.ws = null;
    var input = $('task-search');
    if (clearInput && input) input.value = '';
  }

  function renderSearchResults(box) {
    var st = S.search;
    box.setAttribute('aria-label', '搜索结果');
    var hits = st.hits || [];
    box.appendChild(h('p', { class: 'list-empty search-status', role: 'status', text: searchStatusText(st) }));
    hits.forEach(function (x) {
      var cur = x.session_id === S.sid;
      var item = h('a', {
        class: 'task-item search-hit' + (cur ? ' is-current' : ''),
        href: taskHref(x.session_id, S.mode),
        'aria-current': cur ? 'page' : null,
        title: x.title
      },
        h('span', { class: 'ti-title', text: x.title }),
        h('span', { class: 'ti-meta' },
          h('span', { class: 'ti-time', text: shortDate(x.date) }),
          x.matches.length ? h('span', { class: 'ti-time', text: x.matches.length + ' 处' }) : null));
      x.matches.slice(0, 2).forEach(function (m) {
        var snip = h('span', { class: 'hit-snip' });
        if (m.role) snip.appendChild(h('span', { class: 'hit-role', text: m.role + '：' }));
        highlightParts(m.snippet, st.q).forEach(function (part) {
          snip.appendChild(part.hit ? h('mark', { text: part.t }) : document.createTextNode(part.t));
        });
        item.appendChild(snip);
      });
      box.appendChild(item);
    });
  }

  function focusSearch() {
    if ((window.innerWidth || 1400) <= 760 && !document.body.classList.contains('show-left')) toggleDrawer('left');
    var input = $('task-search');
    if (input) { input.focus(); input.select(); }
  }

  /* ---------------- 导出对话记录 ---------------- */

  function exportTask() {
    if (!S.sid || S.busy.exporting) return;
    var sid = S.sid;
    var fallback = clip((S.view && S.view.title) || '对话记录', 40) + '.md';
    S.busy.exporting = true;
    toast('正在导出对话记录…', 'info');
    function done() { delete S.busy.exporting; }
    // 和下载成品一样，令牌放在地址的 t 参数里；先取回来再存，出错时能给一句明白话，而不是下载一个错误页
    return fetch(exportUrl(sid, S.token), { headers: { 'X-Token': S.token }, cache: 'no-store' }).then(function (r) {
      if (!r.ok) {
        return r.text().then(function (t) {
          var d = null;
          try { d = t ? JSON.parse(t) : null; } catch (e) { d = null; }
          throw new Error(apiErrorMessage(r.status, d));
        });
      }
      var name = dispositionName(r.headers.get('Content-Disposition'), fallback);
      return r.blob().then(function (b) { saveBlob(b, name); });
    }, function () {
      throw new Error('连不上 Agent 程序。它可能已经关闭，请重新运行启动脚本。');
    }).then(done, function (e) { done(); throw e; });
  }

  /** 对话标题旁的"⋯"：取消正在做的事、导出记录、删除。 */
  function openTaskMenu(btn) {
    var v = S.view;
    if (!v || !S.sid) return;
    var st = v.status || 'idle';
    openPop('task-menu', btn, function (m) {
      if (isActive(st) || st === 'paused') {
        m.appendChild(menuItem('取消对话里正在做的事', '已经做完的步骤和文件会保留', function () { return control('cancel'); }));
      }
      m.appendChild(menuItem(S.busy.exporting ? '正在导出…' : '导出对话记录', '存成一个 Markdown 文件', function () { return exportTask(); }));
      m.appendChild(menuItem(rightShown() ? '收起计划与成品' : '查看计划与成品', '', function () { toggleRight(); }));
      m.appendChild(h('div', { class: 'menu-sep', role: 'separator' }));
      m.appendChild(menuItem('删除这个对话', '做出来的文件不会被删除', function () { return deleteTask(); }, true));
    });
  }

  function saveBlob(blob, name) {
    var url = URL.createObjectURL(blob);
    var a = h('a', { href: url, download: name, hidden: true });
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { if (a.parentNode) a.parentNode.removeChild(a); URL.revokeObjectURL(url); }, 10000);
  }

  function updateDocTitle() {
    var n = S.sessions.filter(function (s) { return s.status === 'waiting_approval' || s.status === 'waiting_input'; }).length;
    var base = (S.info && S.info.name) || 'Agent';
    var title = S.route === 'task' && S.view ? (S.view.title || '对话') + ' - ' + base
      : S.route === 'page' ? ((PAGES[parseHash(location.hash).path.split('/')[0]] || {}).title || '') + ' - ' + base : base;
    document.title = (n ? '（' + n + ' 件事等你）' : '') + title;
    // 窄窗口顶栏：对话页写标题；新对话页写现在在哪个模式
    setText($('mobile-title'), S.route === 'task' && S.view ? S.view.title : S.route === 'home' && S.modes.length ? modeName(S.mode) : '');
  }

  /* ---------------- 事件流 ---------------- */

  function connectEvents() {
    if (S.es) { try { S.es.close(); } catch (e) { /* 忽略 */ } S.es = null; }
    if (typeof EventSource === 'undefined') { toast('这个浏览器不支持实时更新，请换用新版 Chrome 或 Edge。'); return; }
    var es;
    try {
      // 第一次连接不带 since：服务端只推之后的实时事件；断线重连时带上最后的 seq，补发中间漏掉的
      es = new EventSource(apiUrl('/api/events', { since: S.lastSeq > 0 ? S.lastSeq : '', t: S.token }));
    } catch (e) { scheduleReconnect(); return; }
    S.es = es;
    es.onopen = function () {
      if (S.esFails > 0) {
        if (S.esFails >= 2) toast('已重新连上。', 'info');
        // 断线期间可能漏了事件（尤其是还没收到过任何 seq 时），重新拉一次最稳妥
        if (S.sid) reloadTask(false).then(null, function () {});
        loadSessions().then(null, function () {});
        if (WEB.sid) webConnect(WEB.sid);
      }
      S.esFails = 0;
      setConn(true);
    };
    es.onmessage = function (m) {
      var ev;
      try { ev = JSON.parse(m.data); } catch (e) { return; }
      try { handleEvent(ev); } catch (e) { if (window.console) console.error(e); }
    };
    es.onerror = function () {
      try { es.close(); } catch (e) { /* 忽略 */ }
      if (S.es === es) S.es = null;
      S.esFails++;
      if (S.esFails >= 2) setConn(false);
      scheduleReconnect();
    };
  }

  function scheduleReconnect() {
    if (S.esTimer) return;
    var wait = Math.min(15000, 800 * Math.pow(2, Math.min(S.esFails, 5)));
    S.esTimer = setTimeout(function () { S.esTimer = null; connectEvents(); }, wait);
  }

  var connEl = null;
  function setConn(ok) {
    if (ok) { if (connEl && connEl.parentNode) connEl.parentNode.removeChild(connEl); connEl = null; return; }
    if (connEl) return;
    connEl = h('div', { class: 'conn-lost', role: 'status', text: '和 Agent 程序的连接断了，正在重连。如果一直连不上，请重新运行启动脚本。' });
    document.body.appendChild(connEl);
  }

  function handleEvent(ev) {
    if (!ev || typeof ev !== 'object') return;
    if (typeof ev.seq === 'number' && ev.seq > S.lastSeq) S.lastSeq = ev.seq;
    // 资料库的收录进度：不属于任何对话
    if (ev.type === 'kb_progress') { onKbProgress(ev.payload && typeof ev.payload === 'object' ? ev.payload : ev); return; }
    if (ev.type === 'kb_cards') { onKbCards(ev.payload && typeof ev.payload === 'object' ? ev.payload : ev); return; }
    var sid = ev.session_id || ev.sid;
    if (ev.type === 'browser_view') { onBrowserView(sid, ev.payload && typeof ev.payload === 'object' ? ev.payload : {}); return; }
    if (sid && sid === S.sid) webOnEvent(ev);
    if (ev.type !== 'assistant_delta') {
      var before = null;
      for (var i = 0; i < S.sessions.length; i++) if (S.sessions[i].id === sid) { before = S.sessions[i].status; break; }
      var r = applyListEvent(S.sessions, ev);
      // 子代理的任务不在列表里，同一个任务只为它刷新一次列表
      if (r.unknown && sid && !S.unknownSeen[sid]) { S.unknownSeen[sid] = true; refreshListSoon(); }
      if (r.changed) markDirty('list');
      if (ev.type === 'status' && sid !== S.sid && before !== null && ev.payload) {
        var st = ev.payload.status;
        if ((st === 'waiting_approval' || st === 'waiting_input') && before !== st) {
          var s = S.sessions.filter(function (x) { return x.id === sid; })[0];
          toast('「' + clip((s && s.title) || '对话', 20) + '」' + statusText(st) + '。', 'info',
            { label: '去看看', run: function () { go('#/task/' + encodeURIComponent(sid)); } });
        }
      }
    }
    if (sid && sid === S.sid && S.view) {
      if (S.loading) { S.buffer.push(ev); return; }
      reduceEvent(S.view, ev);
      markDirty('task');
    }
  }

  function markDirty(part) {
    S.dirty[part] = true;
    if (S.rafPending) return;
    S.rafPending = true;
    var raf = window.requestAnimationFrame || function (f) { return setTimeout(f, 16); };
    raf(flush);
  }

  function flush() {
    S.rafPending = false;
    var d = S.dirty;
    S.dirty = {};
    try {
      if (d.list) { renderTaskList(); updateDocTitle(); }
      if (d.task && S.view && S.route === 'task') renderTask();
    } catch (e) {
      if (window.console) console.error(e);
      if (Date.now() - S.lastRenderError > 5000) { S.lastRenderError = Date.now(); toast('界面更新出错：' + (e.message || e)); }
    }
  }

  /* ---------------- 任务 ---------------- */

  function openTask(sid) {
    if (S.sid !== sid) {
      S.rendered = {};
      S.open = {};
      S.preview = null;
      clear($('timeline'));
    }
    // 输入框里还没发出去的附件属于上一个对话，不能带到这个对话里
    if (S.composerSid !== sid) {
      if (S.files.composer.length || S.images.composer.length) clearAttachments('composer');
      S.composerSid = sid;
    }
    var switched = S.sid !== sid;
    S.sid = sid;
    showView('task');
    if (switched) webOnTask(sid);
    if (!S.view || S.view.sid !== sid) {
      S.view = newView({ id: sid });
      var s = S.sessions.filter(function (x) { return x.id === sid; })[0];
      if (s) { S.view.title = s.title || ''; S.view.status = s.status || 'idle'; }
    }
    renderTask();
    return reloadTask(true);
  }

  function reloadTask(scrollEnd) {
    var sid = S.sid;
    if (!sid) return Promise.resolve();
    S.loading = true;
    S.buffer = [];
    return api('GET', '/api/sessions/' + encodeURIComponent(sid)).then(function (d) {
      if (S.sid !== sid) return;
      d = d || {};
      var session = d.session || { id: sid };
      if (!session.id) session.id = sid;
      // 如果任务不在当前文件夹，切过去，免得左栏对不上
      if (session.workspace_id && session.workspace_id !== S.wsId && S.workspaces.some(function (w) { return w.id === session.workspace_id; })) {
        S.wsId = session.workspace_id;
        try { localStorage.setItem('ca.ws', S.wsId); } catch (e) { /* 忽略 */ }
        resetSearch(true);     // 搜索结果属于原来的文件夹
        renderWorkspace();
        loadSessions().then(null, report);
      }
      // 打开的对话属于另一个模式：总开关跟着对话走，地址改成那个模式的
      if (S.modes.length) {
        var sm = sessionMode(session);
        if (!session.mode) session.mode = sm;
        if (sm !== S.mode && findModeById(S.modes, sm)) applyMode(sm);
        var want = buildHash(sm, 'task/' + encodeURIComponent(sid));
        if (location.hash !== want && history.replaceState) history.replaceState(null, '', location.pathname + location.search + want);
      }
      var v = buildView(session, d.events, d.pending, d.artifacts);
      // "正在整理"的开始事件不落账，重新装载时从旧视图带过来（之后的 done / failed 会收掉它）
      if (S.view && S.view.sid === sid && S.view.compacting) v.compacting = true;
      var buf = S.buffer;
      S.buffer = [];
      S.loading = false;
      buf.forEach(function (ev) { reduceEvent(v, ev); });
      v.sid = sid;
      S.view = v;
      S.rendered = {};
      S.rightSig = null;
      S.bannerSig = null;
      clear($('timeline'));
      for (var i = 0; i < S.sessions.length; i++) {
        if (S.sessions[i].id === sid) { S.sessions[i].status = v.status; if (v.title) S.sessions[i].title = v.title; }
      }
      renderTaskList();
      renderTask({ scrollEnd: scrollEnd !== false, focusAttention: true });
    }, function (e) {
      S.loading = false;
      if (S.sid !== sid) return;
      if (e && e.status === 404) { toast('这个对话已经不存在了。'); go('#/new'); return; }
      throw e;
    });
  }

  function renderTask(opts) {
    opts = opts || {};
    var v = S.view;
    if (!v) return;
    var st = v.status || 'idle';

    setText($('task-title'), v.title || '新对话');
    var tag = $('task-status');
    if (tag) {
      tag.className = 'status st-' + st + (needsAttention(st) ? ' is-need' : '');
      tag.textContent = statusText(st) + (st === 'paused' && v.reason && v.reason !== '用户暂停' ? '：' + v.reason : '');
      // 顶部栏只在运行中、等你处理、出错时挂状态；做完了、没开始就不占地方
      tag.hidden = !(isActive(st) || needsAttention(st) || st === 'failed');
    }
    var hasWork = !!(v.todo && v.todo.length) || Object.keys(v.artifacts || {}).length > 0;
    var hasSrc = Object.keys(v.hits || {}).length > 0;
    var hasRight = hasWork || hasSrc || !!S.preview;
    var rt = $('btn-right-toggle');
    if (rt) {
      var rtName = hasSrc ? (hasWork ? '来源与成品' : '来源') : '计划与成品';
      rt.textContent = rtName;
      rt.hidden = !(hasRight || rightShown());
      rt.setAttribute('aria-expanded', rightShown() ? 'true' : 'false');
      rt.title = rightShown() ? '收起右边的' + rtName : hasSrc ? '在右边看回答引用的原文' : '在右边看计划和做出来的文件';
      var rp = $('rightpane');
      if (rp) rp.setAttribute('aria-label', rtName);
    }
    renderWebToggle();
    renderDeep();
    renderScopeChips('composer');

    renderBanner(v);

    var wrap = $('timeline-wrap');
    var atBottom = !wrap || (wrap.scrollHeight - wrap.scrollTop - wrap.clientHeight < 120);
    renderTimeline(v);
    if (wrap && (opts.scrollEnd || atBottom)) wrap.scrollTop = wrap.scrollHeight;
    if (opts.focusAttention) {
      var card = document.querySelector('#timeline .needs-you');
      if (card && card.scrollIntoView) card.scrollIntoView({ block: 'center' });
    }
    updateJump();

    var input = $('composer-input');
    if (input) {
      // 短一点：窄窗口里也要放得下后面那句"输入 / 看指令"
      input.placeholder = (st === 'waiting_input' ? '直接回答，或点上面的选项'
        : isActive(st) ? '随时补充或改要求'
        : st === 'done' ? '还要做什么？接着说'
        : '接着说点什么') + '。输入 / 看指令';
    }
    updateSendBtn('composer');
    renderRight();
    updateDocTitle();
  }

  function renderBanner(v) {
    var b = $('task-banner');
    if (!b) return;
    var sig = v.sid + '|' + v.status + '|' + v.reason + '|' + (v.compacting ? 'c' : '');
    if (sig === S.bannerSig) return;
    S.bannerSig = sig;
    clear(b);
    b.className = 'banner';
    var st = v.status;
    var msg = '', act = null, loud = false;
    if (v.compacting) {
      msg = '正在整理较早的内容…';
    } else if (st === 'paused') {
      loud = true;
      if (v.reason === '程序重启') msg = '上次运行被中断。已完成的步骤都在下面，点继续接着做。';
      else if (v.reason === '用户暂停' || !v.reason) msg = '已暂停。点继续接着做，也可以先补充要求再继续。';
      else msg = '已暂停：' + v.reason + '。处理好之后点继续。';
      act = { label: '继续', run: function () { return control('resume'); } };
    } else if (st === 'failed') {
      loud = true;
      msg = '出错了' + (v.reason ? '：' + v.reason : '') + '。可以点重试，或者换个说法再发一次。';
      act = { label: '重试', run: function () { return control('resume'); } };
    } else if (st === 'cancelled') {
      msg = '已取消。发一句话可以接着做。';
    }
    if (!msg) { b.hidden = true; return; }
    if (loud) b.classList.add('banner-loud');
    b.appendChild(h('span', { class: 'banner-text', text: msg }));
    if (act) b.appendChild(h('button', { class: 'btn btn-primary btn-sm', type: 'button', text: act.label, on: { click: act.run } }));
    b.hidden = false;
  }

  function control(op) {
    if (!S.sid) return;
    var sid = S.sid;
    if (op === 'cancel' && !window.confirm('取消这个对话里正在做的事？已经做完的步骤和文件会保留。')) return;
    var seq0 = viewSeq(sid);
    return api('POST', '/api/sessions/' + encodeURIComponent(sid) + '/control', { op: op }).then(function () {
      // 请求返回前事件已经到了（模型很快时会这样）：以事件为准，不再改成"运行中"
      if (op === 'resume' && S.view && S.sid === sid && viewSeq(sid) === seq0) { S.view.status = 'running'; S.view.reason = ''; renderTask(); }
    });
  }

  /* ---------- 时间线（按 key 增量更新） ---------- */

  function depOf(it, v) {
    if (it.kind === 'question') return v.status + (it.answered ? '1' : '0') + (S.busy['q' + it.key] ? 'b' : '');
    if (it.kind === 'tool') return (it.state === 'awaiting' ? v.status : '') + '|' + (S.open[it.key] ? '1' : '0') + (S.busy[it.actionId] ? 'b' : '');
    if (it.kind === 'memory') return it.logId != null && S.memUndo[str(it.logId)] ? JSON.stringify(S.memUndo[str(it.logId)]) : '';
    if (it.kind === 'suggest') return (S.busy['sg' + it.key] ? 'b' : '') + (S.suggestSent[it.key] ? 's' : '');
    if (it.kind === 'assistant') {
      // 出处标签要等检索结果；核对结果、反馈、反馈框后到
      var fu = it.seq ? FB.ui[it.seq] : null;
      return Object.keys(v.hits || {}).length + '|' + v.mode + '|' + (it.seq && v.grounding[it.seq] ? 'g' : '') +
        '|' + (it.seq && v.feedback[it.seq] ? v.feedback[it.seq].rating : '') + '|' + (fu ? (fu.open ? 'o' : '') + (fu.busy ? 'b' : '') : '') + (FB.missing ? 'x' : '');
    }
    return '';
  }

  function renderTimeline(v) {
    var tl = $('timeline');
    if (!tl) return;
    if (!v.items.length) {
      clear(tl);
      S.rendered = {};
      tl.appendChild(h('p', { class: 'tl-empty', text: S.loading ? '正在加载…' : '还没有消息。在下面说一件要做的事就开始。' }));
      return;
    }
    var empty = tl.querySelector('.tl-empty');
    if (empty) tl.removeChild(empty);
    var keep = {};
    var prev = null;
    // 一轮里的步骤收成一行，成品收成文件卡片；批准、提问卡片照常单独显示
    groupTimeline(v.items, v.status).forEach(function (entry) {
      var key = entry.key;
      keep[key] = true;
      var rec = S.rendered[key];
      var ver, dep;
      if (entry.type === 'item') { ver = entry.item.v; dep = depOf(entry.item, v); }
      else if (entry.type === 'steps') { ver = 0; dep = stepsSig(entry, S.open); }
      else { ver = 0; dep = filesSig(entry.files); }
      if (!rec || rec.v !== ver || rec.dep !== dep) {
        var el;
        try {
          el = entry.type === 'item' ? renderItem(entry.item, v)
            : entry.type === 'steps' ? renderStepsGroup(entry) : renderFileCards(entry.files);
        } catch (e) {
          if (window.console) console.error(e);
          el = h('div', { class: 'note note-error', text: '这一条显示不出来：' + (e.message || e) });
        }
        // 重画前焦点在这一条里的某个按钮上，重画后放回同一个按钮（展开、收起步骤时不丢焦点）
        var fk = rec && document.activeElement && rec.el.contains(document.activeElement) && document.activeElement.getAttribute
          ? document.activeElement.getAttribute('data-fk') : null;
        if (rec && rec.el.parentNode === tl) tl.replaceChild(el, rec.el);
        rec = S.rendered[key] = { el: el, v: ver, dep: dep };
        if (fk) {
          var again = el.querySelector('[data-fk="' + fk.replace(/["\\]/g, '') + '"]');
          if (again) { try { again.focus({ preventScroll: true }); } catch (e2) { again.focus(); } }
        }
      }
      var want = prev ? prev.nextSibling : tl.firstChild;
      if (want !== rec.el) tl.insertBefore(rec.el, want);
      prev = rec.el;
    });
    Object.keys(S.rendered).forEach(function (k) {
      if (!keep[k]) {
        var r = S.rendered[k];
        if (r.el.parentNode === tl) tl.removeChild(r.el);
        delete S.rendered[k];
      }
    });
  }

  /** 折叠的一行："正在：读取 报表.xlsx…" / "做了 5 步，其中 1 步出错"。点开看每一步。 */
  function renderStepsGroup(entry) {
    var f = entry.fold || foldSteps(entry.steps, {});
    var open = !!S.open[entry.key];
    var box = h('div', { class: 'steps' + (open ? ' is-open' : '') + (f.running ? ' is-running' : '') + (f.failed ? ' has-error' : '') +
      (f.waiting && !f.running ? ' is-waiting' : '') });
    var took = f.secs >= 1 ? formatElapsed(f.secs) : '';
    // 一条细轨：状态点、"做了 5 步"、右边用时和箭头
    box.appendChild(h('button', { class: 'steps-line', type: 'button', 'data-fk': 'g', 'aria-expanded': open ? 'true' : 'false',
      title: open ? '收起' : '展开看每一步',
      on: { click: function () { S.open[entry.key] = !S.open[entry.key]; rerender(); } } },
      h('span', { class: 'steps-dot', 'aria-hidden': 'true' }),
      h('span', { class: 'steps-text', text: f.text }),
      f.note ? h('span', { class: 'steps-note', text: f.note }) : null,
      took ? h('span', { class: 'steps-time', title: '用时', text: took }) : null,
      h('span', { class: 'steps-chev', 'aria-hidden': 'true' }, icon('chevron', 14))));
    if (open) {
      var body = h('div', { class: 'steps-body' });
      entry.steps.forEach(function (it) { body.appendChild(renderStep(it)); });
      box.appendChild(body);
    }
    return box;
  }

  /** 这一轮做出来的文件：跟在回答下面，点了在右边预览，旁边可以下载。 */
  function renderFileCards(files) {
    var box = h('div', { class: 'file-cards' });
    files.forEach(function (f) {
      var name = fileName(f.path);
      box.appendChild(h('div', { class: 'file-card' },
        h('button', { class: 'fc-open', type: 'button', 'data-fk': 'o' + f.path, title: '预览 ' + f.path,
          on: { click: function () { return openPreview(f.path, f.version); } } },
          h('span', { class: 'fc-icon', 'aria-hidden': 'true' }, icon(IC_FILE, 16)),
          h('span', { class: 'fc-text' },
            h('span', { class: 'fc-name', text: name }),
            h('span', { class: 'fc-meta' },
              h('span', { class: 'mono', text: f.path }),
              f.version > 1 ? h('span', { class: 'ver mono', text: 'v' + f.version }) : null))),
        h('a', { class: 'fc-dl', href: fileUrl(f.path, true), download: name, title: '下载 ' + name, 'aria-label': '下载 ' + name }, icon('download', 16))));
    });
    return box;
  }

  function rerender() {
    if (!S.view) return;
    renderTimeline(S.view);
    updateJump();
  }

  function renderItem(it, v) {
    switch (it.kind) {
      case 'user': return renderUser(it);
      case 'assistant': return renderAssistant(it, v);
      case 'tool': return it.state === 'awaiting' ? renderApproval(it, v) : renderStep(it);
      case 'question': return renderQuestion(it, v);
      case 'artifact': return renderArtifactLine(it);
      case 'summary': return renderSummary(it);
      case 'memory': return renderMemoryNote(it);
      case 'skill': return renderSkillNote(it);
      case 'suggest': return renderSuggest(it);
      case 'note': return h('div', { class: 'note note-' + (it.tone || 'notice'), role: it.tone === 'error' ? 'alert' : null },
        h('span', { class: 'note-mark', 'aria-hidden': 'true' }, icon(it.tone === 'error' ? 'alert' : it.tone === 'memory' ? 'memory' : 'info', 12)),
        h('span', { class: 'note-text', text: it.text }));
      default: return h('div', { hidden: true });
    }
  }

  function renderUser(it) {
    var box = h('div', { class: 'msg msg-user' });
    var bubble = h('div', { class: 'bubble' });
    // 技能和附件挂在气泡下面一行，气泡里只放说的话和图片
    var meta = h('div', { class: 'user-meta' });
    if (it.deep) meta.appendChild(h('span', { class: 'skill-tag deep-tag', title: '这一条开了深入研究' }, icon('search', 13), h('span', { text: '深入研究' })));
    if (it.skill) meta.appendChild(h('span', { class: 'skill-tag', title: '点名要用这个技能' }, icon('skill', 13), h('span', { text: '技能：' + it.skill })));
    if (it.text) bubble.appendChild(h('div', { class: 'user-text', text: it.text }));
    var imgs = (it.images || []).filter(function (u) { return typeof u === 'string' && /^data:image\/(png|jpe?g|gif|webp|bmp);base64,/i.test(u); });
    if (imgs.length) {
      var row = h('div', { class: 'user-images' });
      imgs.forEach(function (u) { row.appendChild(h('img', { src: u, alt: '附带的图片', loading: 'lazy' })); });
      bubble.appendChild(row);
    }
    var files = it.files || [];
    if (files.length) {
      var fr = h('div', { class: 'user-files' });
      files.forEach(function (f) {
        fr.appendChild(h('button', { class: 'att att-sent', type: 'button', title: '预览 ' + f.path,
          on: { click: function () { return openPreview(f.path); } } },
          h('span', { class: 'att-icon', 'aria-hidden': 'true' }, icon(IC_FILE, 14)),
          h('span', { class: 'att-text' }, h('span', { class: 'att-name', text: f.name }),
            f.size != null ? h('span', { class: 'att-meta', text: formatSize(f.size) }) : null)));
      });
      meta.appendChild(fr);
    }
    if (bubble.firstChild) box.appendChild(bubble);
    if (meta.firstChild) box.appendChild(meta);
    return box;
  }

  function renderAssistant(it, v) {
    v = v || S.view || { hits: {}, grounding: {}, feedback: {} };
    var box = h('div', { class: 'msg msg-agent' + (it.streaming ? ' is-streaming' : '') });
    var md = h('div', { class: 'md' });
    setMarkdown(md, it.text);
    decorateMarkdown(md);
    var g = it.seq && v.grounding ? v.grounding[it.seq] : null;
    // 出处标签：在渲染好的 DOM 上换文字节点（不另写 innerHTML）
    if (!it.streaming) decorateCites(md, v, g);
    box.appendChild(md);
    if (!it.streaming && str(it.text).trim()) {
      var hs = v.mode === 'hspace';
      var final = !it.calls;          // 带工具调用的是中间的话（"我先查一下"），不写依据
      var hits = v.hits || {};
      var nums = citedNums(it.text, function (n) { return !!hits[n]; });
      if (final && (hs || nums.length || g)) {
        var parts = basisParts(nums.length, hs ? 'hspace' : '', g);
        if (parts.length) box.appendChild(basisLine(parts, nums));
      }
      // 写完的回答下面：复制（悬停或聚焦时出现）；Hspace 的回答还有赞、踩，点过的常显
      var fb = hs && final ? feedbackButtons(v, it) : null;
      var rated = fb && it.seq && v.feedback[it.seq] && v.feedback[it.seq].rating;
      if (hs && final) { var fbx = feedbackBox(v, it); if (fbx) box.appendChild(fbx); }
      box.appendChild(h('div', { class: 'msg-actions' + (rated ? ' has-rating' : '') },
        copyButton('msg-act', function () { return stripThink(str(it.text)).trim(); }, '复制这条回答'), fb));
    }
    return box;
  }

  /** 复制按钮：点了把 get() 的文字放进剪贴板，按钮上短暂显示"已复制"。 */
  function copyButton(cls, get, label) {
    var txt = h('span', { text: '复制' });
    var b = h('button', { class: cls, type: 'button', title: label, 'aria-label': label }, icon('copy', 14), txt);
    b.addEventListener('click', guard(function () {
      return copyText(get()).then(function () {
        b.classList.add('is-done');
        txt.textContent = '已复制';
        setTimeout(function () { b.classList.remove('is-done'); txt.textContent = '复制'; }, 1600);
      }, function () { toast('没能复制。可以手动选中文字再复制。'); });
    }));
    return b;
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText && window.isSecureContext !== false) {
      return navigator.clipboard.writeText(text).then(null, function () { return copyFallback(text); });
    }
    return copyFallback(text);
  }

  function copyFallback(text) {
    return new Promise(function (resolve, reject) {
      var ta = h('textarea', { class: 'sr-only', readonly: true, 'aria-hidden': 'true' });
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      document.body.removeChild(ta);
      if (ok) resolve(); else reject(new Error('copy'));
    });
  }

  var NUM_CELL = /^[\s(（]*[-+−–]?[¥$€£]?\s*\d[\d,，]*(\.\d+)?\s*(%|％|万|亿|元|万元|亿元|倍|个|人|天|次|页|件|k|K|M)?[)）]?\s*$/;

  /** 渲染好的 Markdown 上再加两样（都用 DOM 接口）：代码块右上角的"复制"，表格里数字列右对齐。 */
  function decorateMarkdown(md) {
    var pres = md.querySelectorAll('pre.md-code');
    for (var i = 0; i < pres.length; i++) {
      (function (pre) {
        var code = pre.querySelector('code') || pre;
        pre.appendChild(copyButton('code-copy', function () { return code.textContent; }, '复制这段代码'));
      })(pres[i]);
    }
    var tables = md.querySelectorAll('table');
    for (var t = 0; t < tables.length; t++) {
      var rows = tables[t].rows, cols = {};
      for (var r = 0; r < rows.length; r++) {
        for (var c = 0; c < rows[r].cells.length; c++) {
          var cell = rows[r].cells[c];
          if (cell.tagName !== 'TD') continue;
          var v = cell.textContent.trim();
          if (!v || v === '-' || v === '—') continue;
          if (!cols[c]) cols[c] = { num: 0, all: 0 };
          cols[c].all++;
          if (NUM_CELL.test(v)) cols[c].num++;
        }
      }
      for (var r2 = 0; r2 < rows.length; r2++) {
        for (var c2 = 0; c2 < rows[r2].cells.length; c2++) {
          var k = cols[c2];
          if (k && k.all && k.num === k.all) rows[r2].cells[c2].classList.add('num');
        }
      }
    }
  }

  var STEP_STATE = {
    planned: '准备中', approved: '已批准', running: '进行中', done: '', failed: '没成功',
    voided: '未执行：你改了要求', denied: '你拒绝了', unknown: '结果未知', awaiting: '等你批准'
  };

  function stepMark(state) {
    var cls = 'step-mark sm-' + state;
    if (state === 'done') return h('span', { class: cls, 'aria-hidden': 'true' }, svgCheck());
    if (state === 'failed' || state === 'denied') return h('span', { class: cls, 'aria-hidden': 'true', text: '×' });
    if (state === 'unknown') return h('span', { class: cls, 'aria-hidden': 'true', text: '?' });
    if (state === 'voided') return h('span', { class: cls, 'aria-hidden': 'true', text: '–' });
    return h('span', { class: cls, 'aria-hidden': 'true' });
  }

  function svgCheck() {
    var ns = 'http://www.w3.org/2000/svg';
    var svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', '0 0 12 12');
    svg.setAttribute('width', '10');
    svg.setAttribute('height', '10');
    var p = document.createElementNS(ns, 'path');
    p.setAttribute('d', 'M2.5 6.2l2.3 2.3 4.7-5');
    p.setAttribute('fill', 'none');
    p.setAttribute('stroke', 'currentColor');
    p.setAttribute('stroke-width', '1.6');
    svg.appendChild(p);
    return svg;
  }

  function renderStep(it) {
    var info = stepText(it);
    if (info.hidden && it.state !== 'failed') return h('div', { class: 'step-hidden', hidden: true });
    var disp = displayOf(it.display);
    var open = !!S.open[it.key];
    var box = h('div', { class: 'step st-' + it.state + (open ? ' is-open' : '') + (it.looped ? ' is-looped' : '') + (it.tool === 'tool_load' ? ' is-load' : '') });
    var line = h('button', { class: 'step-line', type: 'button', 'data-fk': 's' + it.key, 'aria-expanded': open ? 'true' : 'false',
      title: open ? '收起' : '展开看详细参数和结果',
      on: { click: function () { S.open[it.key] = !S.open[it.key]; rerender(); } } },
      stepMark(it.looped ? 'voided' : it.state),
      info.icon ? h('span', { class: 'step-kind', 'aria-hidden': 'true' }, icon(info.icon, 14)) : null,
      h('span', { class: 'step-verb' + (disp ? ' is-display' : ''), text: info.verb }),
      info.target ? h('span', { class: 'step-target mono', text: info.target }) : null,
      it.hits ? h('span', { class: 'step-target', text: it.hits.length ? '找到 ' + it.hits.length + ' 条' : '没找到' }) : null,
      // 第 3.8 节：重复的调用被程序拦下了
      it.looped ? h('span', { class: 'step-state step-loop', title: '同样的调用连续 ' + it.looped + ' 次结果都一样，这一次没有执行，让它换个做法',
        text: '重复的调用，已拦下' })
        : STEP_STATE[it.state] ? h('span', { class: 'step-state', text: STEP_STATE[it.state] }) : null,
      h('span', { class: 'step-chev', 'aria-hidden': 'true' }));
    box.appendChild(line);
    if ((it.state === 'failed' || it.state === 'unknown') && it.result && !open && !it.looped) {
      box.appendChild(h('div', { class: 'step-why', text: clip(it.result, 160) }));
    }
    if (open) {
      var body = h('div', { class: 'step-body' });
      var facts = disp ? displayFacts(disp) : null;
      if (facts && facts.children.length) body.appendChild(facts);
      if (it.hits && it.hits.length) {
        body.appendChild(h('div', { class: 'kv-caption', text: '找到的片段' }));
        body.appendChild(kbHitList(it.hits));
      }
      body.appendChild(h('div', { class: 'kv-caption', text: '参数' }));
      body.appendChild(h('pre', { class: 'raw mono', text: prettyArgs(it) }));
      if (it.parseError) body.appendChild(h('p', { class: 'step-why', text: '参数有问题：' + it.parseError }));
      if (it.result != null) {
        body.appendChild(h('div', { class: 'kv-caption', text: '结果' }));
        body.appendChild(h('pre', { class: 'raw mono', text: prettyResult(it.result) }));
        if (it.truncated) body.appendChild(h('p', { class: 'muted small', text: '结果很长，这里只显示了一部分；完整内容已存到文件夹的 .agent 目录里。' }));
      } else if (it.state === 'running') {
        body.appendChild(h('p', { class: 'muted small', text: '还在执行…' }));
      }
      body.appendChild(h('p', { class: 'muted small mono', text: it.tool + (it.actionId ? '  ' + it.actionId : '') }));
      box.appendChild(body);
    }
    return box;
  }

  /** 浏览器 / 桌面步骤展开后：页面标题、网站（或程序）、要填的文字。 */
  function displayFacts(d) {
    var dl = h('dl', { class: 'step-facts' });
    var isWeb = d.kind === 'browser';
    if (d.page) append(dl, [h('dt', { text: isWeb ? '页面' : '窗口' }), h('dd', { text: d.page })]);
    if (d.host) append(dl, [h('dt', { text: isWeb ? '网站' : '程序' }), h('dd', { class: 'mono', text: d.host })]);
    if (d.text) append(dl, [h('dt', { text: '填的文字' }), h('dd', { class: 'fill-text', text: d.text })]);
    return dl;
  }

  function prettyArgs(it) {
    if (it.args && Object.keys(it.args).length) {
      try { return JSON.stringify(it.args, null, 2); } catch (e) { /* 继续往下 */ }
    }
    return str(it.rawArgs) || '（没有参数）';
  }

  var ARG_LABEL = {
    path: '文件', content: '内容', old: '原来的文字', new: '改成', src: '原位置', dst: '新位置', code: '代码',
    markdown: '正文', query: '关键词', url: '网址', sheets: '工作表', slides: '幻灯片', title: '标题', subtitle: '副标题',
    text: '内容', when: '时间', prompt: '要做的事', task: '调查内容', path_a: '表 A', path_b: '表 B', sheet_a: '表 A 工作表',
    sheet_b: '表 B 工作表', key_a: '表 A 主键', key_b: '表 B 主键', compare: '比较的列', output_path: '结果存到', name: '名称',
    glob: '文件名规则', scope: '范围', items: '清单', question: '问题', options: '选项',
    action: '做法', target: '记在哪', old_text: '原来那条', description: '描述', body: '正文', session_id: '对话', around: '位置'
  };

  /** 浏览器 / 桌面动作的批准卡片：一句话的动作、页面标题和网站、要填的文字。参数收在"细节"里。 */
  function renderDisplayApproval(it, v, d) {
    var isDelete = it.risk === 'delete', isSubmit = it.risk === 'submit';
    var card = h('section', { class: 'approve needs-you' + (isDelete ? ' is-delete' : isSubmit ? ' is-submit' : ''),
      'aria-label': '需要你批准：' + d.title });
    card.appendChild(h('header', { class: 'approve-head' },
      h('span', { class: 'approve-kind', 'aria-hidden': 'true' }, icon(d.kind === 'desktop' ? 'monitor' : 'globe', 16)),
      h('span', { class: 'approve-title', text: d.title }),
      isDelete || isSubmit ? h('span', { class: 'risk risk-' + it.risk, text: riskLabel(it.risk) }) : null,
      h('span', { class: 'need-tag', text: '等你批准' })));
    if (d.page || d.host) {
      card.appendChild(h('p', { class: 'approve-where' },
        d.page ? h('span', { class: 'approve-page', text: d.page }) : null,
        d.host ? h('span', { class: 'approve-host mono', text: d.host }) : null));
    }
    if (d.text) {
      card.appendChild(h('div', { class: 'approve-fill' },
        h('span', { class: 'arg-k', text: '要填的文字' }), h('span', { class: 'arg-v', text: d.text })));
    }
    var note = {
      'delete': '这一步会在网页上删除或付款，做了可能撤不回来。',
      submit: '提交以后可能撤不回来。先在网页面板里看一眼填的内容。',
      external: it.tool === 'browser_open' ? '这个网站不在允许的名单里。' : '会访问公司外部的网络或系统。',
      exec: '会操作这台电脑上的程序。'
    }[it.risk];
    if (note) card.appendChild(h('p', { class: 'approve-note', text: note }));
    var raw = prettyArgs(it);
    if (it.args && Object.keys(it.args).length) {
      card.appendChild(h('div', { class: 'approve-args' }, h('details', { class: 'arg-long' },
        h('summary', {}, h('span', { text: '细节' }), h('span', { class: 'muted small', text: '（' + it.tool + ' 的参数）' })),
        h('pre', { class: 'raw mono', text: raw }))));
    }
    card.appendChild(approveActions(it, v, isDelete));
    return card;
  }

  /** 批准卡片底下的按钮：批准、拒绝、（能给时）以后都允许。 */
  function approveActions(it, v, isDelete) {
    var busy = !!S.busy[it.actionId];
    var off = busy || !it.actionId;
    var actions = h('div', { class: 'approve-actions' });
    var web = !!displayOf(it.display);
    actions.appendChild(h('button', { class: 'btn ' + (isDelete ? 'btn-danger' : 'btn-primary'), type: 'button', disabled: off,
      text: isDelete && !web ? '批准删除' : '批准', on: { click: function () { return decide(it, true, false); } } }));
    actions.appendChild(h('button', { class: 'btn', type: 'button', disabled: off, text: '拒绝',
      on: { click: function () { return decide(it, false, false); } } }));
    var always = alwaysOption(it);
    if (always) {
      actions.appendChild(h('button', { class: 'btn btn-ghost', type: 'button', disabled: off,
        text: always.label, title: always.title || ('以后「' + toolInfo(it.tool, it.args, it.risk).verb + '」不再询问，可以在设置里改回来'),
        on: { click: function () { return decide(it, true, true); } } }));
    }
    if (v.status === 'paused') actions.appendChild(h('span', { class: 'muted small', text: '对话已暂停。批准后点「继续」才会执行。' }));
    return actions;
  }

  function renderApproval(it, v) {
    var disp = displayOf(it.display);
    if (disp) return renderDisplayApproval(it, v, disp);
    var info = toolInfo(it.tool, it.args, it.risk);
    var isDelete = it.risk === 'delete';
    var args = it.args || {};
    var isSkill = it.tool === 'skill_save';
    var card = h('section', { class: 'approve needs-you' + (isDelete ? ' is-delete' : ''), 'aria-label': '需要你批准：' + info.verb });
    // 一句话说它想做什么；右边"等你批准"；下面是对象（路径、文件名）和一句说明
    card.appendChild(h('header', { class: 'approve-head' },
      h('span', { class: 'approve-title', text: isSkill ? '存成技能' : info.verb }),
      h('span', { class: 'need-tag', text: '等你批准' })));
    if (isSkill) {
      card.appendChild(h('p', { class: 'approve-target', text: str(args.name) || '（没有填名字）' }));
      if (args.description) card.appendChild(h('p', { class: 'approve-note', text: str(args.description) }));
      card.appendChild(h('p', { class: 'approve-note', text: '保存后，以后遇到同类的事，Agent 会照这个做法做。不想要了可以在技能页删除。' }));
    } else if (info.target) card.appendChild(h('p', { class: 'approve-target', text: info.target }));
    var note = isSkill ? '' : {
      'delete': '文件会先移到文件夹里的回收目录（.agent/trash），需要时可以找回。',
      overwrite: '会改动已有的文件。改之前会自动留一份旧版本。',
      exec: '会在这台电脑上运行一段代码，最长 2 分钟。',
      external: '会访问公司外部的网络或系统。',
      read_outside: '要读的文件不在当前文件夹里，只读不改。想以后都直接读，把批准方式改成「全放行」。'
    }[it.risk];
    if (note) card.appendChild(h('p', { class: 'approve-note', text: note }));

    // 参数：短的直接列出，长的折叠。保存技能时只把正文放在一个可以滚动的等宽文本块里
    var details = h('div', { class: 'approve-args' });
    if (isSkill) {
      var skillBody = str(args.body);
      details.appendChild(h('div', { class: 'kv-caption', text: '正文' + (skillBody ? '（' + skillBody.length + ' 字）' : '') }));
      details.appendChild(h('pre', { class: 'raw mono skill-pre', tabindex: '0', 'aria-label': '技能正文', text: skillBody || '（没有正文）' }));
    }
    if (!isSkill) Object.keys(args).forEach(function (k) {
      var val = args[k];
      var text = typeof val === 'string' ? val : prettyResult(str(val));
      var label = ARG_LABEL[k] || k;
      if (text.length > 120 || text.indexOf('\n') >= 0) {
        details.appendChild(h('details', { class: 'arg-long' },
          h('summary', {}, h('span', { text: label }), h('span', { class: 'muted small', text: '（' + text.length + ' 字，点开查看）' })),
          h('pre', { class: 'raw mono', text: text })));
      } else if (!(text && info.target && info.target.indexOf(text) >= 0)) {
        details.appendChild(h('div', { class: 'arg-short' },
          h('span', { class: 'arg-k', text: label }), h('span', { class: 'arg-v mono', text: text })));
      }
    });
    if (details.children.length) card.appendChild(details);
    // 保存技能会影响以后所有同类任务，每次都单独问，不给"以后都允许"（alwaysOption 里判断）
    card.appendChild(approveActions(it, v, isDelete));
    return card;
  }

  function decide(it, allow, always) {
    if (!it.actionId) return;
    var aid = it.actionId;
    S.busy[aid] = true;
    rerender();
    return api('POST', '/api/actions/' + encodeURIComponent(aid) + '/decide', { allow: !!allow, always: !!always }).then(function () {
      delete S.busy[aid];
      if (it.state === 'awaiting') { it.state = allow ? 'approved' : 'denied'; touch(it); }
      if (allow && always && it.tool === 'browser_open') {
        var d = displayOf(it.display);
        toast(d && d.host ? '以后打开 ' + d.host + ' 不再询问。' : '以后打开这个网站不再询问。', 'info');
      }
      markDirty('task');
    }, function (e) {
      delete S.busy[aid];
      rerender();
      throw e;
    });
  }

  function renderQuestion(it, v) {
    var live = !it.answered && v.status === 'waiting_input';
    var card = h('section', { class: 'ask' + (live ? ' needs-you' : ' is-done'), 'aria-label': '需要你回答' });
    if (live) card.appendChild(h('header', { class: 'approve-head' }, h('span', { class: 'approve-title', text: '问你一件事' }),
      h('span', { class: 'need-tag', text: '等你回答' })));
    card.appendChild(h('p', { class: 'ask-q', text: it.question }));
    if (live) {
      if (it.options && it.options.length) {
        var opts = h('div', { class: 'ask-options' });
        it.options.forEach(function (o) {
          opts.appendChild(h('button', { class: 'btn btn-option', type: 'button', text: o, disabled: !!S.busy['q' + it.key],
            on: { click: function () { return answer(it, o); } } }));
        });
        card.appendChild(opts);
      }
      card.appendChild(h('p', { class: 'muted small', text: '也可以直接在下面的输入框里回答。' }));
    } else if (it.answered) {
      card.appendChild(h('p', { class: 'ask-a' }, h('span', { class: 'muted', text: '你的回答：' }), h('span', { text: it.answer || '（已回答）' })));
    } else {
      card.appendChild(h('p', { class: 'muted small', text: '这个问题还没有回答。' }));
    }
    return card;
  }

  function answer(it, text) {
    if (!S.sid) return;
    S.busy['q' + it.key] = true;
    rerender();
    return api('POST', '/api/sessions/' + encodeURIComponent(S.sid) + '/message', { text: text }).then(function () {
      delete S.busy['q' + it.key];
      markDirty('task');
    }, function (e) { delete S.busy['q' + it.key]; rerender(); throw e; });
  }

  function renderArtifactLine(it) {
    return h('div', { class: 'note note-artifact' },
      h('span', { class: 'note-mark', 'aria-hidden': 'true' }, icon('check', 12)),
      h('span', { class: 'note-text' },
        h('span', { text: it.version > 1 ? '更新了 ' : '做好了 ' }),
        h('button', { class: 'linklike mono', type: 'button', text: it.path, title: '在右边预览',
          on: { click: function () { return openPreview(it.path, it.version); } } }),
        it.version > 1 ? h('span', { class: 'ver mono', text: 'v' + it.version }) : null));
  }

  function renderSummary(it) {
    var d = h('details', { class: 'summary-block' }, h('summary', { text: '较早的过程已整理成摘要' }));
    var md = h('div', { class: 'md' });
    setMarkdown(md, it.text);
    d.appendChild(md);
    return d;
  }

  /* ---------- 时间线：记忆、技能、提示 ---------- */

  var MEM_ICON = { add: 'memory', replace: 'memory', remove: 'x' };

  function renderMemoryNote(it) {
    var u = it.logId != null ? S.memUndo[str(it.logId)] : null;
    var undone = !!it.undone || !!(u && u.state === 'done');
    var text = memoryNoteText(it);
    var box = h('div', { class: 'note note-memory' + (undone ? ' is-undone' : '') },
      h('span', { class: 'note-mark', 'aria-hidden': 'true' }, icon(MEM_ICON[it.op] || 'memory', 12)),
      h('span', { class: 'note-text' },
        h('span', { class: 'mem-note-text', text: text, title: it.op === 'replace' && it.before ? '原来是：' + it.before : null }),
        undone ? h('span', { class: 'sr-only', text: '（已撤销）' }) : null,
        !undone && u && u.state === 'error' ? h('span', { class: 'note-why', role: 'alert', text: u.msg }) : null));
    if (it.logId != null) {
      var act = h('span', { class: 'note-act' });
      if (undone) act.appendChild(h('span', { class: 'tag', text: '已撤销' }));
      else {
        act.appendChild(h('button', { class: 'btn btn-ghost btn-sm', type: 'button', disabled: !!(u && u.state === 'busy'),
          text: u && u.state === 'busy' ? '正在撤销…' : '撤销', 'aria-label': '撤销：' + clip(text, 40),
          on: { click: function () { return undoMemory(it.logId, rerender); } } }));
      }
      box.appendChild(act);
    }
    return box;
  }

  /** 撤销一次记忆改动。结果记在 S.memUndo 里，after() 负责重画。出错时把后端那句话显示出来，不再往外抛。 */
  function undoMemory(logId, after) {
    var id = str(logId);
    var cur = S.memUndo[id];
    if (cur && (cur.state === 'busy' || cur.state === 'done')) return;
    S.memUndo[id] = { state: 'busy' };
    after();
    return api('POST', '/api/memories/undo', { log_id: logId }).then(function () {
      S.memUndo[id] = { state: 'done' };
      after(true);
    }, function (e) {
      S.memUndo[id] = { state: 'error', msg: (e && e.message) || '撤销没有成功。' };
      after(false);
    });
  }

  function renderSkillNote(it) {
    return h('div', { class: 'note note-skill' },
      h('span', { class: 'note-mark', 'aria-hidden': 'true' }, icon('skill', 12)),
      h('span', { class: 'note-text' },
        h('span', { text: (it.updated ? '更新了技能：' : '存成了技能：') }),
        h('span', { class: 'mono', text: it.name || '（没有名字）' }),
        it.description ? h('span', { class: 'muted', text: '  ' + clip(it.description, 80) }) : null),
      h('span', { class: 'note-act' }, h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '去技能页',
        'aria-label': '到技能页查看 ' + (it.name || ''), on: { click: function () { go('#/skills'); } } })));
  }

  function renderSuggest(it) {
    var sent = !!S.suggestSent[it.key];
    var busy = !!S.busy['sg' + it.key];
    var box = h('div', { class: 'note note-suggest' },
      h('span', { class: 'note-mark', 'aria-hidden': 'true' }, icon('spark', 12)),
      h('span', { class: 'note-text', text: it.text }));
    if (it.what === 'save_skill') {
      var act = h('span', { class: 'note-act' });
      if (it.done) act.appendChild(h('span', { class: 'tag', text: '已存成技能' }));
      else if (sent) act.appendChild(h('span', { class: 'tag', text: '已发送' }));
      else {
        act.appendChild(h('button', { class: 'btn btn-sm', type: 'button', disabled: busy, text: busy ? '正在发送…' : '存成技能',
          title: '在这个对话里让 Agent 把这次的做法存成技能，保存前还会问你',
          on: { click: function () { return saveAsSkill(it); } } }));
      }
      box.appendChild(act);
    }
    return box;
  }

  function saveAsSkill(it) {
    if (!S.sid || S.busy['sg' + it.key]) return;
    var key = 'sg' + it.key;
    S.busy[key] = true;
    rerender();
    return postMessage(S.sid, { text: '把这次的做法存成技能' }).then(function () {
      delete S.busy[key];
      S.suggestSent[it.key] = true;
      rerender();
    }, function (e) { delete S.busy[key]; rerender(); throw e; });
  }

  function updateJump() {
    var btn = $('jump-attention'), wrap = $('timeline-wrap');
    if (!btn) return;
    if (!wrap || !S.view || S.route !== 'task') { btn.hidden = true; if (wrap) wrap.classList.remove('has-jump'); return; }
    var cards = document.querySelectorAll('#timeline .needs-you');
    if (!cards.length) { btn.hidden = true; wrap.classList.remove('has-jump'); return; }
    var wr = wrap.getBoundingClientRect();
    var visible = false;
    for (var i = 0; i < cards.length; i++) {
      var r = cards[i].getBoundingClientRect();
      if (r.bottom > wr.top + 24 && r.top < wr.bottom - 24) { visible = true; break; }
    }
    btn.textContent = cards.length === 1 ? '有 1 件事等你确认' : '有 ' + cards.length + ' 件事等你确认';
    btn.hidden = visible;
    // 按钮浮在对话底部时，让它下面的文字淡出，免得叠在一起看不清
    wrap.classList.toggle('has-jump', !visible);
  }

  function jumpToAttention() {
    var card = document.querySelector('#timeline .needs-you');
    if (!card) return;
    if (card.scrollIntoView) card.scrollIntoView({ block: 'center', behavior: reducedMotion() ? 'auto' : 'smooth' });
    var b = card.querySelector('button:not([disabled])');
    if (b) setTimeout(function () { try { b.focus({ preventScroll: true }); } catch (e) { b.focus(); } }, 60);
  }

  function reducedMotion() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  /* ---------------- 右栏：计划、成品、详情、预览 ---------------- */

  function renderRight() {
    var v = S.view;
    if (!v) return;
    var lists = $('rp-lists'), pv = $('rp-preview');
    show(lists, !S.preview);
    show(pv, !!S.preview);
    if (S.preview) return;
    // 内容没变就不重画：流式输出时每帧都会走到这里，重画会抢走右栏里按钮的焦点
    var cur = S.sessions.filter(function (x) { return x.id === v.sid; })[0] || {};
    var srcs = Object.keys(v.hits || {}).length ? sourceList(v) : null;
    var sig = JSON.stringify([v.sid, v.todo, Object.keys(v.artifacts).map(function (k) { return [k, v.artifacts[k].length]; }),
      v.steps, v.revision, v.endpoint, S.wsId, cur.updated, cur.created,
      srcs ? srcs.list.map(function (x) { return x.n; }) : null, srcs && srcs.cited, S.srcFocus, Object.keys(v.grounding || {}).length]);
    if (sig === S.rightSig) return;
    S.rightSig = sig;

    // 来源：这个对话引用过的片段（Hspace 的脸面，放最上面）
    var srcSec = $('rp-sources-sec');
    show(srcSec, !!srcs);
    if (srcs) renderSources($('rp-sources'), v);
    // Hspace 的对话里，空的"计划""成品"不占地方
    var quiet = v.mode === 'hspace' || !!srcs;
    show($('rp-todo-sec'), !(quiet && !(v.todo && v.todo.length)));
    show($('rp-art-sec'), !(quiet && !Object.keys(v.artifacts || {}).length));

    var todo = clear($('rp-todo'));
    if (todo) {
      if (!v.todo || !v.todo.length) {
        todo.appendChild(h('p', { class: 'rp-empty', text: '事情超过三步时，Agent 会在这里列出计划，做完一步勾一步。' }));
      } else {
        var ol = h('ol', { class: 'todo' });
        var done = 0;
        v.todo.forEach(function (t) {
          var s = (t && t.status) || 'pending';
          if (s === 'done') done++;
          ol.appendChild(h('li', { class: 'todo-item td-' + s },
            h('span', { class: 'todo-mark', 'aria-hidden': 'true' }, s === 'done' ? svgCheck() : null),
            h('span', { class: 'todo-text', text: t && t.text }),
            s === 'doing' ? h('span', { class: 'todo-now', text: '正在做' }) : null,
            h('span', { class: 'sr-only', text: s === 'done' ? '（已完成）' : s === 'doing' ? '（正在做）' : '（未开始）' })));
        });
        todo.appendChild(h('p', { class: 'rp-count mono', text: done + ' / ' + v.todo.length }));
        todo.appendChild(ol);
      }
    }

    var arts = clear($('rp-artifacts'));
    if (arts) {
      var paths = Object.keys(v.artifacts || {});
      if (!paths.length) {
        arts.appendChild(h('p', { class: 'rp-empty', text: '做出来的文件会出现在这里，点一下就能预览。' }));
      } else {
        paths.sort(function (a, b) {
          var la = v.artifacts[a][v.artifacts[a].length - 1], lb = v.artifacts[b][v.artifacts[b].length - 1];
          return (lb.ts || 0) - (la.ts || 0);
        });
        var ul = h('ul', { class: 'artifacts' });
        paths.forEach(function (p) {
          var vers = v.artifacts[p];
          var last = vers[vers.length - 1];
          ul.appendChild(h('li', { class: 'art' },
            h('button', { class: 'art-open', type: 'button', title: '预览 ' + p, on: { click: function () { return openPreview(p, last.version); } } },
              h('span', { class: 'art-name', text: fileName(p) }),
              h('span', { class: 'art-path mono', text: p })),
            h('span', { class: 'art-side' },
              h('span', { class: 'ver mono', text: 'v' + last.version }),
              h('a', { class: 'art-dl', href: fileUrl(p, true), download: fileName(p), title: '下载 ' + fileName(p), text: '下载' }))));
        });
        arts.appendChild(ul);
      }
    }

    var det = clear($('rp-details'));
    if (det) {
      var s = S.sessions.filter(function (x) { return x.id === v.sid; })[0] || {};
      var w = ws();
      var dl = h('dl', { class: 'details' });
      function row(k, val, mono) {
        if (val === '' || val == null) return;
        dl.appendChild(h('dt', { text: k }));
        dl.appendChild(h('dd', { class: mono ? 'mono' : null, text: val }));
      }
      row('文件夹', w ? w.name : '');
      row('开始于', formatTime(s.created));
      row('最后更新', formatTime(s.updated));
      row('模型回复', v.steps ? v.steps + ' 次' : '');
      row('改过要求', v.revision > 1 ? (v.revision - 1) + ' 次' : (s.revision > 1 ? (s.revision - 1) + ' 次' : ''));
      row('模型服务', v.endpoint, true);
      row('对话编号', v.sid, true);
      det.appendChild(dl);
      det.appendChild(h('button', { class: 'btn btn-ghost btn-sm btn-danger-text', type: 'button', text: '删除这个对话',
        on: { click: deleteTask } }));
    }
  }

  function deleteTask() {
    if (!S.sid) return;
    if (!window.confirm('删除这个对话的记录？做出来的文件不会被删除。')) return;
    var sid = S.sid;
    return api('DELETE', '/api/sessions/' + encodeURIComponent(sid)).then(function () {
      S.sessions = S.sessions.filter(function (x) { return x.id !== sid; });
      go('#/new');
      toast('对话已删除。', 'info');
    });
  }

  function openPreview(path, version) {
    S.preview = { path: path, version: version, loading: true, data: null, error: '' };
    // 窄屏时右栏是抽屉，打开它；宽屏时右栏默认收起，这次临时打开（不记成习惯）
    if (!isWide()) { document.body.classList.add('show-right'); syncScrim(); }
    else if (!S.rightOpen) { S.rightOpen = true; applyRight(); }
    renderPreview();
    if (S.view) renderTask();
    var want = S.preview;
    return api('GET', apiUrl('/api/preview', { workspace: S.wsId, path: path })).then(function (d) {
      if (S.preview !== want) return;
      want.loading = false;
      want.data = d || {};
      renderPreview();
    }, function (e) {
      if (S.preview !== want) return;
      want.loading = false;
      want.error = (e && e.message) || '预览失败';
      renderPreview();
    });
  }

  function closePreview() {
    S.preview = null;
    S.rightSig = null;
    renderRight();
  }

  var KIND_TEXT = { xlsx: 'Excel 表格', docx: 'Word 文档', pptx: 'PPT', pdf: 'PDF', text: '文本', md: 'Markdown', csv: 'CSV 表格' };

  function renderPreview() {
    var pv = $('rp-preview');
    if (!pv || !S.preview) return;
    show($('rp-lists'), false);
    show(pv, true);
    clear(pv);
    var p = S.preview;
    if (p.kind === 'part') { renderPartPreview(pv, p); return; }
    pv.appendChild(h('div', { class: 'pv-head' },
      h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '返回', on: { click: closePreview } }),
      h('a', { class: 'btn btn-sm', href: fileUrl(p.path, true), download: fileName(p.path), text: '下载' })));
    pv.appendChild(h('div', { class: 'pv-title' },
      h('span', { class: 'pv-name', text: fileName(p.path) }),
      p.version ? h('span', { class: 'ver mono', text: 'v' + p.version }) : null));
    pv.appendChild(h('div', { class: 'pv-path mono', text: p.path }));
    if (p.loading) { pv.appendChild(h('p', { class: 'muted', text: '正在读取…' })); return; }
    if (p.error) { pv.appendChild(h('p', { class: 'form-error', text: p.error })); return; }
    var d = p.data || {};
    var kind = d.kind ? (KIND_TEXT[d.kind] || d.kind) : '';
    if (kind) pv.appendChild(h('p', { class: 'muted small', text: '类型：' + kind + (d.truncated ? '。内容较长，只显示前一部分，完整内容请下载查看。' : '') }));
    var text = str(d.text);
    pv.appendChild(text ? h('pre', { class: 'pv-text', text: text }) : h('p', { class: 'muted', text: '这个文件没有可以显示的文字。可以下载后用对应的软件打开。' }));
  }

  /* ---------------- 输入框 ---------------- */

  function composerFor(which) {
    return which === 'home'
      ? { input: $('home-input'), send: $('home-send'), thumbs: $('home-images'), files: $('home-files'), mode: $('home-mode'), plus: $('home-plus') }
      : { input: $('composer-input'), send: $('composer-send'), thumbs: $('composer-images'), files: $('composer-files'), mode: $('composer-mode'), plus: $('composer-plus') };
  }

  function autosize(el) {
    if (!el) return;
    el.style.height = 'auto';
    var max = Math.max(120, Math.floor(window.innerHeight * 0.4));
    el.style.height = Math.min(el.scrollHeight + 2, max) + 'px';
  }

  function bindComposer(which) {
    var c = composerFor(which);
    if (!c.input) return;
    c.input.setAttribute('aria-controls', 'slash-menu');
    c.input.setAttribute('aria-autocomplete', 'list');
    c.input.setAttribute('aria-expanded', 'false');
    c.input.addEventListener('keydown', guard(function (e) {
      if (hashKey(which, e)) return;
      if (slashKey(which, e)) return;
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
        e.preventDefault();
        // 回车只发送，不当"停止"用：输入框空着时按回车什么都不做
        if (!hasContent(which)) return;
        return send(which);
      }
    }));
    c.input.addEventListener('input', function () { if (which !== 'home') autosize(c.input); updateSendBtn(which); slashUpdate(which); hashUpdate(which); });
    // 输入法组字结束时 input 事件可能已经过去了，补一次
    c.input.addEventListener('compositionend', function () { slashUpdate(which); hashUpdate(which); });
    // 光标左右移动、点到别处：# 菜单跟着开关
    c.input.addEventListener('keyup', function (e) { if (/^(ArrowLeft|ArrowRight|Home|End)$/.test(e.key)) hashUpdate(which); });
    c.input.addEventListener('click', function () { if (HP.which === which || hashQuery(c.input.value, c.input.selectionStart)) hashUpdate(which); });
    c.input.addEventListener('blur', function () {
      setTimeout(function () {
        if (SL.which === which && document.activeElement !== c.input) closeSlash();
        if (HP.which === which && document.activeElement !== c.input) closeHash();
      }, 0);
    });
    c.input.addEventListener('paste', guard(function (e) {
      var cd = e.clipboardData;
      var files = [];
      var items = (cd && cd.items) || [];
      for (var i = 0; i < items.length; i++) {
        if (items[i].kind === 'file') { var f = items[i].getAsFile(); if (f) files.push(f); }
      }
      if (!files.length && cd && cd.files && cd.files.length) files = Array.prototype.slice.call(cd.files);
      if (files.length) { e.preventDefault(); addFiles(which, files); }
    }));
    if (c.send) c.send.addEventListener('click', guard(function () { return send(which); }));
    if (c.plus) c.plus.addEventListener('click', guard(function (e) { openPlusMenu(which, e.currentTarget); }));
  }

  /** 输入框所在的对话现在是什么状态。新对话页永远是 idle。 */
  function composerStatus(which) {
    return which === 'composer' && S.sid && S.view ? (S.view.status || 'idle') : 'idle';
  }

  function hasContent(which) {
    var c = composerFor(which);
    return !!(c.input && c.input.value.trim()) || S.images[which].length > 0 || S.files[which].length > 0;
  }

  /** 发送按钮：有内容时是"发送"（箭头）；对话正在做事、输入框空着时是"停止"。上传中不能发。 */
  function updateSendBtn(which) {
    var c = composerFor(which);
    if (!c.send) return;
    var mode = sendMode(composerStatus(which), hasContent(which));
    var att = attachState(S.files[which]);
    var busy = !!S.busy['send-' + which];
    var stop = mode === 'stop';
    c.send.classList.toggle('is-stop', stop);
    var label = stop ? '停止' : '发送';
    var tip = stop ? '停下来（之后再发一句话或点「继续」接着做）'
      : att.uploading ? '文件还在上传，传完才能发送' : '发送（回车）';
    c.send.setAttribute('aria-label', label);
    c.send.title = tip;
    c.send.disabled = busy || (!stop && att.uploading > 0);
  }

  var MAX_IMAGES = 4, MAX_IMAGE_BYTES = 5 * 1024 * 1024;

  function addImages(which, files) {
    var list = S.images[which];
    // 当前模型看不了图：不加进附件，直接告诉用户后端给的那句原话
    var g = gate('image_input');
    if (!g.ok && files && files.length) { toast(gateText(g, '看图片附件') + g.fallback); return; }
    Array.prototype.forEach.call(files, function (f) {
      if (!/^image\/(png|jpe?g|gif|webp|bmp)$/.test(f.type)) { toast('只能添加 PNG、JPG、GIF、WebP 图片。'); return; }
      if (f.size > MAX_IMAGE_BYTES) { toast('图片「' + f.name + '」超过 5 MB，换一张小一点的。'); return; }
      if (list.length >= MAX_IMAGES) { toast('一次最多带 ' + MAX_IMAGES + ' 张图片。'); return; }
      var slot = { name: f.name, url: '' };
      list.push(slot);
      var r = new FileReader();
      r.onload = function () { slot.url = String(r.result || ''); renderThumbs(which); };
      r.onerror = function () { S.images[which] = S.images[which].filter(function (x) { return x !== slot; }); toast('读不了这张图片。'); renderThumbs(which); };
      r.readAsDataURL(f);
    });
    renderThumbs(which);
  }

  function renderThumbs(which) {
    var box = composerFor(which).thumbs;
    if (!box) return;
    clear(box);
    var list = S.images[which];
    box.hidden = !list.length;
    list.forEach(function (img) {
      box.appendChild(h('span', { class: 'thumb' },
        img.url && /^data:image\//.test(img.url) ? h('img', { src: img.url, alt: img.name }) : h('span', { class: 'thumb-wait', text: '…' }),
        h('button', { class: 'thumb-x', type: 'button', 'aria-label': '去掉这张图片', text: '×',
          on: { click: function () { S.images[which] = S.images[which].filter(function (x) { return x !== img; }); renderThumbs(which); } } })));
    });
    updateSendBtn(which);
  }

  var MAX_FILES = 20;      // 后端一条消息最多收 20 个附件
  var fileSeq = 0;

  /** 添加文件（菜单、拖入、粘贴都走这里）：图片走看图的路径，其他上传到当前空间的"上传"文件夹。 */
  function addFiles(which, list) {
    var parts = splitFiles(list);
    if (parts.images.length) addImages(which, parts.images);
    if (!parts.others.length) return;
    if (S.uploadMissing) { toast(UPLOAD_MISSING); return; }
    if (!S.wsId) { toast('先添加一个文件夹。'); return; }
    var limit = uploadLimit(S.info);
    parts.others.forEach(function (f) {
      if (S.files[which].length >= MAX_FILES) { toast('一次最多带 ' + MAX_FILES + ' 个文件。'); return; }
      if (!f.size && f.type === '' && !/\.[^.\/]+$/.test(f.name || '')) {
        toast('「' + (f.name || '这个') + '」像是一个文件夹。要处理整个文件夹，用"+"里的"用一个文件夹工作"。');
        return;
      }
      if (f.size > limit) { toast(tooBigText(f.name, limit)); return; }
      var slot = { id: ++fileSeq, name: str(f.name) || '文件', size: f.size, state: 'uploading', path: '', error: '', file: f, ws: S.wsId };
      S.files[which].push(slot);
      uploadOne(which, slot);
    });
    renderFiles(which);
  }

  function uploadOne(which, slot) {
    slot.state = 'uploading';
    slot.error = '';
    var alive = function () { return S.files[which].indexOf(slot) >= 0; };
    return fetch(uploadUrl(slot.ws, slot.name), {
      method: 'POST', cache: 'no-store', body: slot.file,
      headers: { 'X-Token': S.token, 'Content-Type': 'application/octet-stream' }
    }).then(function (r) {
      return r.text().then(function (txt) {
        var d = null;
        try { d = txt ? JSON.parse(txt) : null; } catch (e) { d = null; }
        if (!r.ok) {
          var msg = uploadErrorText(r.status, d);
          if (msg === UPLOAD_MISSING) S.uploadMissing = true;
          throw new Error(msg);
        }
        if (!d || !d.path) throw new Error('上传没有成功，请再试一次。');
        return d;
      });
    }, function () {
      throw new Error('连不上 Agent 程序，文件没有传上去。');
    }).then(function (d) {
      if (!alive()) return;
      slot.state = 'done';
      slot.path = str(d.path);
      if (d.name) slot.name = str(d.name);
      if (d.size != null && !isNaN(d.size)) slot.size = Number(d.size);
      slot.file = null;
      renderFiles(which);
    }, function (e) {
      if (!alive()) return;
      slot.state = 'error';
      slot.error = (e && e.message) || '上传没有成功。';
      renderFiles(which);
    });
  }

  function removeFile(which, slot) {
    S.files[which] = S.files[which].filter(function (x) { return x !== slot; });
    renderFiles(which);
    var c = composerFor(which);
    if (c.input) c.input.focus();
  }

  /** 输入框上方的文件小卡片：名字、大小、×；上传中转圈，失败给一句话和"重试"。 */
  function renderFiles(which) {
    var box = composerFor(which).files;
    if (box) {
      clear(box);
      var list = S.files[which];
      box.hidden = !list.length;
      list.forEach(function (f) {
        var meta = f.state === 'uploading' ? '正在上传…' : f.state === 'error' ? '没传上去' : (f.size != null ? formatSize(f.size) : '');
        var chip = h('div', { class: 'att is-' + f.state, title: f.state === 'error' ? f.error : (f.path || f.name),
          role: f.state === 'error' ? 'alert' : null },
          h('span', { class: 'att-icon', 'aria-hidden': 'true' }, f.state === 'uploading' ? h('span', { class: 'att-spin' }) : icon(IC_FILE, 14)),
          h('span', { class: 'att-text' },
            h('span', { class: 'att-name', text: f.name }),
            h('span', { class: 'att-meta' + (f.state === 'error' ? ' att-err' : ''), text: f.state === 'error' ? f.error : meta })));
        if (f.state === 'error' && f.file && !S.uploadMissing) {
          chip.appendChild(h('button', { class: 'att-retry', type: 'button', text: '重试', 'aria-label': '重新上传「' + f.name + '」',
            on: { click: function () { uploadOne(which, f); renderFiles(which); } } }));
        }
        chip.appendChild(h('button', { class: 'att-x', type: 'button', 'aria-label': '去掉「' + f.name + '」', title: '去掉', text: '×',
          on: { click: function () { removeFile(which, f); } } }));
        box.appendChild(chip);
      });
    }
    updateSendBtn(which);
  }

  /** 清掉一个输入框的附件（发出去之后、换了对话之后）。 */
  function clearAttachments(which) {
    S.images[which] = [];
    S.files[which] = [];
    renderThumbs(which);
    renderFiles(which);
  }

  function pickFiles(which) {
    var f = $('file-any');
    if (!f) return;
    f.setAttribute('data-for', which);
    f.click();
  }

  /** 输入框左下的"+"：添加文件、添加图片、用一个文件夹工作。 */
  function openPlusMenu(which, btn) {
    openPop('plus-menu', btn, function (m) {
      m.appendChild(menuItem('添加文件', '表格、文档、PDF 都可以', function () { pickFiles(which); }));
      var img = gate('image_input');
      var imgItem = menuItem('添加图片', img.ok ? '截图、照片，让它看图' : gateText(img, '看图片附件'), function () { pickImages(which); });
      if (!img.ok) {
        imgItem.disabled = true;
        imgItem.setAttribute('aria-disabled', 'true');
        if (img.fallback) imgItem.appendChild(h('span', { class: 'mi-desc mi-fallback', text: img.fallback }));
      }
      m.appendChild(imgItem);
      m.appendChild(h('div', { class: 'menu-sep', role: 'separator' }));
      m.appendChild(menuItem('用一个文件夹工作…', which === 'composer' ? '在那个文件夹里开一个新对话' : '让它读写你电脑上的一个文件夹',
        function () { return connectFolder(); }));
      m.appendChild(h('div', { class: 'menu-sep', role: 'separator' }));
      m.appendChild(menuItem('指令…', '新对话、整理、记住、换主题等', function () { openSlashFromButton(which); }));
    });
  }

  /** 往一个已有任务里发一条消息。输入框发送和"存成技能"按钮都走这里。 */
  function postMessage(sid, body) {
    var seq0 = viewSeq(sid);
    return api('POST', '/api/sessions/' + encodeURIComponent(sid) + '/message', body).then(function () {
      // 先乐观地显示"运行中"；但如果请求返回前事件已经到了（模型很快时会这样），以事件为准，
      // 否则会把已经做完的对话一直显示成"运行中"
      if (S.view && S.sid === sid && viewSeq(sid) === seq0 && !isActive(S.view.status)) { S.view.status = 'running'; S.view.reason = ''; markDirty('task'); }
    });
  }

  /** 当前打开的对话已经收到的最后一个事件序号；不是这个对话就返回 -1。 */
  function viewSeq(sid) {
    return S.view && S.sid === sid && typeof S.view.lastSeq === 'number' ? S.view.lastSeq : -1;
  }

  /** 发送。opts = {text, skill}：技能指令用，text 代替输入框里的文字。 */
  function send(which, opts) {
    var c = composerFor(which);
    if (!c.input || S.busy['send-' + which]) return;
    var status = composerStatus(which);
    // 正在做事、输入框空着：这个按钮是"停止"
    if (sendMode(status, hasContent(which)) === 'stop') return stopTask(which);
    if (V.state !== 'idle' && V.which === which) {
      toast(V.state === 'transcribing' ? '正在识别语音，识别完再发送。' : '正在录音。先点麦克风按钮结束录音，看一眼文字再发送。', 'info');
      return;
    }
    // 输入框里是指令：执行它（技能指令会带着 opts 回到这里）；不是任何指令就照常发出去
    if (!opts) {
      var sr = slashSubmit(which);
      if (sr) return sr.then ? sr : undefined;
    }
    opts = opts || {};
    var skill = opts.skill || '';
    var text = opts.text != null ? str(opts.text).trim() : c.input.value.trim();
    var imgs = S.images[which];
    if (imgs.some(function (x) { return !x.url; })) { toast('图片还在读取，稍等一下再发。', 'info'); return; }
    var att = attachState(S.files[which]);
    if (att.uploading) { toast('文件还在上传，传完再发。', 'info'); return; }
    if (att.failed) { toast('有文件没传上去。点「重试」，或者点 × 去掉它再发。'); return; }
    if (!S.wsId) { toast('先添加一个文件夹。'); return; }
    // 文件传到了当时所在的空间；空间换了，路径就对不上了
    var stale = S.files[which].filter(function (f) { return f.ws !== S.wsId; });
    if (stale.length) {
      S.files[which] = S.files[which].filter(function (f) { return f.ws === S.wsId; });
      renderFiles(which);
      toast('换了文件夹，有 ' + stale.length + ' 个文件没有带过来。需要的话再添加一次。');
      return;
    }
    var images = imgs.map(function (x) { return x.url; });
    var files = attachState(S.files[which]).paths;
    if (!text && !images.length && !files.length && !skill) { c.input.focus(); return; }
    S.busy['send-' + which] = true;
    updateSendBtn(which);
    closeSlash();
    var body = messageBody(text, { images: images, files: files, skill: skill });
    // 深入研究：开关开着或者 /深入，只对这一条有效
    var deep = S.mode === 'hspace' && (opts.deep || S.deep[which]);
    body = withDepth(body, deep);
    var p;
    if (which === 'home' || !S.sid) {
      var wsId = S.wsId;
      var extra = { workspace_id: wsId };
      // 新建对话时带上模式和（Hspace 里）选中的资料库；老后端不带
      if (S.modes.length) {
        extra.mode = S.mode;
        if (S.mode === 'hspace') Object.assign(extra, scopeBody(getScope('home')));
      }
      p = api('POST', '/api/sessions', Object.assign(extra, body)).then(function (s) {
        s = s && s.session ? s.session : s;
        if (!s || !s.id) throw new Error('对话没有建起来，请再试一次。');
        if (!s.title) s.title = clip(text || (files.length ? fileName(files[0]) : '') || (skill ? '技能：' + skill : ''), 20);
        if (S.modes.length && !s.mode) s.mode = extra.mode;
        if (S.wsId === wsId && !S.sessions.some(function (x) { return x.id === s.id; })) S.sessions.unshift(s);
        return s;
      }).then(function (s) {
        clearComposer(which);
        go('#/task/' + encodeURIComponent(s.id));
      });
    } else {
      var sid = S.sid;
      p = postMessage(sid, body).then(function () { if (S.sid === sid) clearComposer(which); });
    }
    return p.then(function () {
      delete S.busy['send-' + which];
      if (deep) { S.deep[which] = false; renderDeep(); }
      updateSendBtn(which);
    }, function (e) {
      delete S.busy['send-' + which];
      updateSendBtn(which);
      // 后端以 400 拒绝（例如模型看不了图）：原话照常显示，输入框里的文字和附件都留着；能力可能变了，重新取一次
      if (e && e.status === 400 && images.length) loadCaps();
      throw e;
    });
  }

  function clearComposer(which) {
    var c = composerFor(which);
    if (c.input) { c.input.value = ''; if (which !== 'home') autosize(c.input); }
    clearAttachments(which);
  }

  /** 发送按钮变成"停止"时：等同于原来的"暂停"。停下后再发一句话或点"继续"接着做。 */
  function stopTask(which) {
    if (!S.sid) return;
    S.busy['send-' + which] = true;
    updateSendBtn(which);
    return Promise.resolve(control('pause')).then(function () {
      delete S.busy['send-' + which];
      updateSendBtn(which);
    }, function (e) {
      delete S.busy['send-' + which];
      updateSendBtn(which);
      throw e;
    });
  }

  /* ---------------- 斜杠指令 ----------------
   * 两个输入框共用：输入框第一个字符是 / ／ 、时在输入框上方弹出菜单（放不下就放下面）。
   * 写名字时按名字筛选；名字后面有空格时是在写参数：只显示那一个指令（批准、主题列出可选项）。
   * Esc 关掉后这一段 / 当普通文字，直到输入框不再以 / 开头。
   */

  var SL = {
    which: null,        // 菜单开在哪个输入框上：home / composer；关着是 null
    rows: [],           // 显示的行 {type: cmd|opt|info, cmd, label, desc, run, current}
    sel: -1,            // 选中的行（只在可选的行之间移动）
    spaced: false,      // 在写参数了
    off: { home: false, composer: false },   // 按过 Esc
    skills: null,       // GET /api/skills 的缓存
    skillsAt: 0,
    skillsLoading: false,
    card: null          // 小卡片开在哪个输入框上
  };

  function slashInput(which) { return composerFor(which).input; }

  function slashCtx(which) {
    return { session: which === 'composer' && !!S.sid, status: composerStatus(which), mode: S.mode };
  }

  function slashList(which) {
    return slashCommands(slashCtx(which), SL.skills, S.templates);
  }

  /** 技能列表一分钟内用缓存；回来后菜单还开着就重画。 */
  function loadSlashSkills() {
    if (SL.skillsLoading || (SL.skills && Date.now() - SL.skillsAt < 60000)) return;
    SL.skillsLoading = true;
    api('GET', '/api/skills').then(function (d) {
      SL.skills = normalizeSkills(d);
      SL.skillsAt = Date.now();
      SL.skillsLoading = false;
      if (SL.which) slashUpdate(SL.which);
    }, function () { SL.skillsLoading = false; SL.skills = SL.skills || []; SL.skillsAt = Date.now(); });
  }

  /** 批准、主题不带参数时列出的可选项。 */
  function slashOptions(cmd, which) {
    if (cmd.id === 'mode') {
      var cur = S.info.approval_mode || 'auto';
      return MODES.map(function (m) {
        return { type: 'opt', label: m.label, desc: m.desc, current: m.id === cur, keys: m.id + ' ' + m.label,
          run: function () { return setMode(m.id); } };
      });
    }
    if (cmd.id === 'theme') {
      var T = themeApi();
      if (!T) return [{ type: 'info', label: '主题还没有装好。' }];
      var pref = '';
      try { pref = str(T.pref()); } catch (e) { pref = ''; }
      var list = [{ id: 'auto', name: '跟随系统', desc: '浅色系统用素白，深色系统用墨夜' }].concat(T.list || []);
      return list.map(function (t) {
        return { type: 'opt', label: str(t.name) || str(t.id), desc: t.desc || (t.id === 'auto' ? '' : (t.dark ? '深色' : '浅色')),
          current: str(t.id) === (pref || 'auto'), keys: str(t.id) + ' ' + str(t.name),
          run: function () { return setTheme(str(t.id), true); } };
      });
    }
    return [];
  }

  /** 输入框内容变了：决定菜单开不开、显示什么。 */
  function slashUpdate(which) {
    var input = slashInput(which);
    if (!input) return;
    var p = parseSlash(input.value);
    if (!p) { SL.off[which] = false; if (SL.which === which) closeSlash(); return; }
    if (SL.off[which]) return;
    if (document.activeElement !== input) return;
    loadSlashSkills();
    var list = slashList(which);
    var rows = [];
    if (!p.spaced) {
      filterSlash(list, p.name).forEach(function (c) { rows.push({ type: 'cmd', cmd: c }); });
      if (!rows.length) {
        var b = findSlash(SLASH_COMMANDS, p.name);
        rows.push({ type: 'info', label: b ? '现在用不了 /' + b.name + '：' + availText(b, slashCtx(which)) : '没有这个指令，回车会当普通消息发送' });
      }
    } else {
      var cmd = findSlash(list, p.name);
      if (!cmd) {
        var b2 = findSlash(SLASH_COMMANDS, p.name);
        rows.push({ type: 'info', label: b2 ? '现在用不了 /' + b2.name + '：' + availText(b2, slashCtx(which)) : '没有这个指令，回车会当普通消息发送' });
      } else if (cmd.pick) {
        var opts = slashOptions(cmd, which);
        var q = p.arg.toLowerCase();
        var shown = q ? opts.filter(function (o) { return o.type === 'opt' && matchScore(q, o.keys) > 0; }) : opts;
        rows.push({ type: 'cmd', cmd: cmd, head: true });
        if (shown.length) rows = rows.concat(shown);
        else rows.push({ type: 'info', label: '没有叫「' + p.arg + '」的' + (cmd.id === 'theme' ? '主题' : '批准方式') });
      } else {
        rows.push({ type: 'cmd', cmd: cmd, head: true });
      }
    }
    SL.spaced = p.spaced;
    SL.rows = rows;
    var first = -1;
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].type === 'opt' || (rows[i].type === 'cmd' && !rows[i].head)) { first = i; break; }
    }
    // 写参数时：可选项里优先选当前生效的那个之外的第一项；没有可选项时不选（回车走发送）
    SL.sel = first;
    SL.which = which;
    drawSlash();
  }

  function availText(c, ctx) {
    if (c.when === 'idle-or-new') return '正在做事时用不了，先 /停止';
    if (c.when !== 'any' && !(ctx && ctx.session)) return '先打开一个对话';
    if (c.when === 'running') return '只在正在做事时能用';
    if (c.when === 'stopped') return '只在停下或出错时能用';
    if (c.when === 'idle') return '正在做事时用不了，先 /停止';
    return '先打开一个对话';
  }

  function slashRowId(i) { return 'slash-opt-' + i; }

  function drawSlash() {
    var menu = $('slash-menu');
    var which = SL.which;
    var input = which ? slashInput(which) : null;
    if (!menu || !input) return;
    clear(menu);
    var lastGroup = null;
    var GROUP = { cmd: '指令', skill: '技能', tpl: '模板' };
    SL.rows.forEach(function (r, i) {
      if (r.type === 'cmd' && !r.head && !SL.spaced && r.cmd.group !== lastGroup) {
        lastGroup = r.cmd.group;
        menu.appendChild(h('div', { class: 'slash-group', role: 'presentation', text: GROUP[lastGroup] || '' }));
      }
      menu.appendChild(slashRow(r, i));
    });
    if (SL.spaced && SL.rows[0] && SL.rows[0].head && !SL.rows[0].cmd.pick) {
      var c0 = SL.rows[0].cmd;
      menu.appendChild(h('div', { class: 'slash-foot', role: 'presentation',
        text: c0.group === 'skill' ? '写上要求（也可以不写），回车发送' : c0.argNeed ? '写好' + c0.arg.replace(/[<>]/g, '') + '后回车' : '回车执行' }));
    } else {
      menu.appendChild(h('div', { class: 'slash-foot', role: 'presentation', text: '上下键选择，回车或 Tab 确认，Esc 关闭' }));
    }
    menu.hidden = false;
    input.setAttribute('aria-expanded', 'true');
    if (SL.sel >= 0) input.setAttribute('aria-activedescendant', slashRowId(SL.sel));
    else input.removeAttribute('aria-activedescendant');
    placeSlash(menu, which);
    var cur = SL.sel >= 0 ? $(slashRowId(SL.sel)) : null;
    if (cur && cur.scrollIntoView) cur.scrollIntoView({ block: 'nearest' });
  }

  function slashRow(r, i) {
    var sel = i === SL.sel;
    var selectable = r.type === 'opt' || (r.type === 'cmd' && !r.head);
    var attrs = { class: 'slash-item slash-' + r.type + (r.head ? ' is-head' : '') + (sel ? ' is-sel' : '') + (r.current ? ' is-current' : ''),
      id: slashRowId(i), role: 'option', 'aria-selected': sel ? 'true' : 'false' };
    if (!selectable) attrs['aria-disabled'] = 'true';
    attrs.on = {
      mousedown: function (e) { e.preventDefault(); },
      click: function () { if (selectable) { SL.sel = i; return slashConfirm(SL.which, false); } },
      mousemove: function () { if (selectable && SL.sel !== i) { SL.sel = i; drawSlash(); } }
    };
    var el = h('div', attrs);
    if (r.type === 'info') { el.appendChild(h('span', { class: 'slash-desc', text: r.label })); return el; }
    if (r.type === 'opt') {
      el.appendChild(h('span', { class: 'slash-name' }, h('span', { text: r.label }),
        r.current ? h('span', { class: 'slash-alias', text: '当前' }) : null));
      if (r.desc) el.appendChild(h('span', { class: 'slash-desc', text: r.desc }));
      return el;
    }
    var c = r.cmd;
    el.appendChild(h('span', { class: 'slash-name' },
      h('span', { class: 'slash-cmd', text: c.group === 'tpl' ? c.name : '/' + c.name }),
      c.alias ? h('span', { class: 'slash-alias', text: '/' + c.alias }) : null,
      c.arg ? h('span', { class: 'slash-arg', text: c.arg }) : null));
    el.appendChild(h('span', { class: 'slash-desc', text: c.desc }));
    return el;
  }

  /** 菜单贴着输入框：上面放得下放上面，否则放下面；宽度和输入框一样，窄窗口下不超出。 */
  function placeSlash(el, which) {
    var input = slashInput(which);
    var box = input && input.closest ? input.closest('.composer') || input : input;
    if (!box) return;
    var r = box.getBoundingClientRect();
    var vw = window.innerWidth || 1200, vh = window.innerHeight || 800;
    var w = Math.min(Math.max(r.width, 280), vw - 16);
    el.style.width = w + 'px';
    el.style.left = Math.max(8, Math.min(r.left, vw - w - 8)) + 'px';
    var above = r.top - 8 - 6, below = vh - r.bottom - 8 - 6;
    var room = Math.max(above, below);
    el.style.maxHeight = Math.max(120, Math.min(room, 420)) + 'px';
    var mh = el.offsetHeight;
    if (above >= mh || above >= below) el.style.top = Math.max(8, r.top - 6 - mh) + 'px';
    else el.style.top = (r.bottom + 6) + 'px';
  }

  function closeSlash() {
    var menu = $('slash-menu');
    var which = SL.which;
    SL.which = null;
    SL.rows = [];
    SL.sel = -1;
    if (menu && !menu.hidden) { menu.hidden = true; clear(menu); }
    var input = which ? slashInput(which) : null;
    if (input) { input.setAttribute('aria-expanded', 'false'); input.removeAttribute('aria-activedescendant'); }
  }

  /** 输入框上的按键。返回 true 表示菜单处理了。 */
  function slashKey(which, e) {
    if (e.isComposing || e.keyCode === 229) return false;     // 输入法正在组字：回车、Tab、上下键都归输入法
    if (SL.card === which && e.key === 'Escape') { closeSlashCard(true); e.preventDefault(); e.stopPropagation(); return true; }
    if (SL.which !== which) return false;
    var k = e.key;
    if (k === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      SL.off[which] = true;
      closeSlash();
      return true;
    }
    if (k === 'ArrowDown' || k === 'ArrowUp') {
      var idx = [];
      SL.rows.forEach(function (r, i) { if (r.type === 'opt' || (r.type === 'cmd' && !r.head)) idx.push(i); });
      if (!idx.length) return false;
      e.preventDefault();
      var at = idx.indexOf(SL.sel);
      if (at < 0) at = k === 'ArrowDown' ? 0 : idx.length - 1;
      else at = (at + (k === 'ArrowDown' ? 1 : idx.length - 1)) % idx.length;
      SL.sel = idx[at];
      drawSlash();
      return true;
    }
    if ((k === 'Enter' && !e.shiftKey) || (k === 'Tab' && !e.shiftKey)) {
      if (SL.sel < 0) {
        if (k === 'Tab') { e.preventDefault(); return true; }
        return false;     // 没有可选的行：回车照常走发送（未知指令当普通消息、带参数的指令在那里执行）
      }
      e.preventDefault();
      var res = slashConfirm(which, k === 'Tab');
      if (res && typeof res.then === 'function') res.then(null, report);
      return true;
    }
    return false;
  }

  /** 确认选中的一行。 */
  function slashConfirm(which, byTab) {
    var r = SL.rows[SL.sel];
    var input = slashInput(which);
    if (!r || !input) return;
    if (r.type === 'opt') {
      setInput(which, '');
      closeSlash();
      return r.run();
    }
    if (r.type !== 'cmd') return;
    var c = r.cmd;
    var fill = slashFill(c);
    if (fill == null) {
      // 不带参数的指令：Tab 只补全名字，回车立即执行
      if (byTab) { setInput(which, '/' + c.name); slashUpdate(which); return; }
      setInput(which, '');
      closeSlash();
      return runSlash(which, c, '');
    }
    if (c.group === 'tpl') { setInput(which, ''); closeSlash(); return useTemplate(which, c.tpl || { title: c.name, prompt: c.prompt }); }
    setInput(which, fill);
    slashUpdate(which);
  }

  function setInput(which, text) {
    var input = slashInput(which);
    if (!input) return;
    input.value = text;
    try { input.focus(); input.setSelectionRange(text.length, text.length); } catch (e) { /* 忽略 */ }
    if (which !== 'home') autosize(input);
    updateSendBtn(which);
  }

  /** 发送前：输入框里是指令就执行。返回 假 = 不是指令，照常发送；真或 Promise = 已处理。 */
  function slashSubmit(which) {
    var input = slashInput(which);
    if (!input || SL.off[which]) return false;
    var r = resolveSlash(input.value, slashList(which));
    if (!r || r.kind === 'unknown') return false;
    if (r.kind === 'unavailable') { toast('现在用不了 /' + r.cmd.name + '：' + availText(r.cmd, slashCtx(which)) + '。', 'info'); return true; }
    if (r.kind === 'need-arg') {
      toast('/' + r.cmd.name + ' 后面要写' + r.cmd.arg.replace(/[<>]/g, '') + '。', 'info');
      setInput(which, '/' + r.cmd.name + ' ');
      slashUpdate(which);
      return true;
    }
    var c = r.cmd;
    if (c.group === 'tpl') { setInput(which, ''); closeSlash(); useTemplate(which, c.tpl || { title: c.name, prompt: c.prompt }); return true; }
    if (c.pick && !r.arg) { setInput(which, '/' + c.name + ' '); SL.off[which] = false; slashUpdate(which); return true; }
    var before = input.value;
    if (c.group !== 'skill') { setInput(which, ''); closeSlash(); }
    var p = runSlash(which, c, r.arg);
    if (p && typeof p.then === 'function' && c.group !== 'skill') {
      // 没做成（比如后端是旧版本）：把刚才写的放回输入框，免得重打
      return p.then(null, function (e) {
        var i = slashInput(which);
        if (i && !i.value) setInput(which, before);
        throw e;
      });
    }
    return p && p.then ? p : true;
  }

  /** "+"菜单里的"指令…"：在输入框里放一个 / 并打开菜单。 */
  function openSlashFromButton(which) {
    var input = slashInput(which);
    if (!input) return;
    SL.off[which] = false;
    var v = input.value;
    if (!parseSlash(v)) setInput(which, v.trim() ? '/' : '/');
    else setInput(which, v);
    slashUpdate(which);
  }

  /** 缺少新接口（后端还是旧版本）时的说法。 */
  function needBackend(e, what) {
    if (e && (e.missing || e.status === 404 && /没有这个接口/.test(e.message || ''))) {
      return new Error(what + '需要更新后端。请把 Agent 程序更新到新版，重新运行启动脚本后再试。');
    }
    return e;
  }

  /** 执行一个指令。 */
  function runSlash(which, c, arg) {
    if (c.group === 'skill') return send(which, { text: arg, skill: c.skill });
    if (c.group === 'tpl') { return useTemplate(which, c.tpl || { title: c.name, prompt: c.prompt }); }
    switch (c.id) {
      case 'new': go('#/new'); return;
      case 'stop': return stopTask(which);
      case 'continue': return control('resume');
      case 'compact': return compactSession(arg);
      case 'rename': return renameSession(arg);
      case 'export': return exportTask();
      case 'delete': return deleteTask();
      case 'search': return slashSearch(arg);
      case 'remember': return rememberNow(arg);
      case 'memory': case 'skills': case 'schedule': case 'kb': case 'settings': go('#/' + c.id); return;
      case 'library': go('#/kb'); return;
      case 'deep': return send(which, { text: arg, deep: true });
      case 'scope': return slashScope(which, arg);
      case 'save-skill':
        if (!S.sid) return;
        return postMessage(S.sid, { text: '把这次的做法存成技能' });
      case 'file': return pickFiles(which);
      case 'folder': return connectFolder();
      case 'mode': {
        var m = matchMode(MODES, arg);
        if (!m) { toast('没有「' + arg + '」这种批准方式。可选：每步都问、自动、全放行。', 'info'); return; }
        return setMode(m.id);
      }
      case 'theme': {
        var T = themeApi();
        if (!T) { toast('主题还没有装好。', 'info'); return; }
        var id = matchTheme(T.list, arg);
        if (!id) { toast('没有叫「' + arg + '」的主题。打 /主题 看全部。', 'info'); return; }
        return setTheme(id, true);
      }
      case 'status': return showStatus(which);
      case 'help': return showHelp(which);
    }
  }

  /** /范围：不带参数时弹出勾选菜单；带资料库名（或"全部"）时直接设好。对话里改的是这个对话，新对话页改的是下一个对话。 */
  function slashScope(which, arg) {
    var target = which === 'composer' && S.sid ? 'session' : 'home';
    return loadLibraries().then(function (libs) {
      if (S.libsMissing) { toast('程序还没有资料库功能。', 'info'); return; }
      if (!libs.length) { toast('还没有资料库。先在知识库里加一个。', 'info', { label: '去知识库', run: function () { go('#/kb'); } }); return; }
      if (!arg) {
        var anchor = target === 'home' ? (document.querySelector('#home-kb .scope-btn') || composerFor(which).input) : composerFor(which).input;
        openScopeMenu(anchor, target);
        return;
      }
      var ids = matchLibraries(libs, arg);
      if (!ids) { toast('没有叫「' + arg + '」的资料库。有：' + libs.map(function (l) { return l.name; }).join('、') + '。', 'info'); return; }
      if (target === 'session' && S.view) {
        var sid = S.view.sid;
        // 老后端：只发 libraries（和原来一样）；新后端走 setScope（标签、文件保留）
        if (!scopeV2()) {
          return api('PATCH', '/api/sessions/' + encodeURIComponent(sid), { libraries: ids }).then(function () {
            if (S.view && S.view.sid === sid) S.view.libraries = ids;
            toast('这个对话接下来查：' + scopeText(libs, ids) + '。', 'info');
          });
        }
        var cur = getScope('composer'); cur.libraries = ids;
        return setScope('composer', cur);
      }
      var hc = getScope('home'); hc.libraries = ids;
      setScope('home', hc);
      toast('下一个对话查：' + scopeLabel(scopeLibs(), getScope('home'), S.docNames) + '。', 'info');
    });
  }

  function compactSession(focus) {
    if (!S.sid) return;
    var sid = S.sid;
    var body = { op: 'compact' };
    if (focus) body.focus = focus;
    return api('POST', '/api/sessions/' + encodeURIComponent(sid) + '/control', body).then(function (r) {
      if (r && r.ok === false) throw new Error(r.error ? str(r.error) : '这次没有整理成。');
      toast(compactText(r), 'info');
    }, function (e) {
      if (e && e.status === 409) throw new Error('正在做事时不能整理。等它停下，或先 /停止 再整理。');
      if (e && e.status === 400 && /op 只能是/.test(e.message || '')) e.missing = true;
      throw needBackend(e, '整理');
    });
  }

  function renameSession(title) {
    if (!S.sid) return;
    var t = str(title).replace(/\s+/g, ' ').trim();
    if (!t) { toast('写上新标题。', 'info'); return; }
    if (t.length > 60) { toast('标题最多 60 个字，现在是 ' + t.length + ' 个。', 'info'); return; }
    var sid = S.sid;
    return api('PATCH', '/api/sessions/' + encodeURIComponent(sid), { title: t }).then(function (s) {
      var nt = s && s.title ? str(s.title) : t;
      S.sessions.forEach(function (x) { if (x.id === sid) x.title = nt; });
      if (S.view && S.sid === sid) { S.view.title = nt; renderTask(); }
      renderTaskList();
      toast('标题改成了「' + nt + '」。', 'info');
    }, function (e) { throw needBackend(e, '改名'); });
  }

  function slashSearch(q) {
    var input = $('task-search');
    if (!input) return;
    focusSearch();
    input.value = q;
    onSearchInput();
  }

  /** /记住：直接加一条到"关于我"，不经过模型。成功后的提示带撤销。 */
  function rememberNow(text) {
    var t = str(text).trim();
    if (!t) { toast('写上要记住的内容。', 'info'); return; }
    return api('POST', '/api/memories', { text: t, target: 'user', workspace_id: null }).then(function (r) {
      var lid = r && r.log_id != null ? r.log_id : null;
      var msg = '记住了：' + clip(t, 40);
      if (lid == null) { toast(msg, 'info'); return; }
      toast(msg, 'info', { label: '撤销', run: function () {
        return undoMemory(lid, function (ok) {
          if (ok === true) toast('已撤销。', 'info');
          else if (ok === false) toast((S.memUndo[str(lid)] || {}).msg || '撤销没有成功。');
        });
      } });
    });
  }

  /* ---------- 主题：主题同事提供 window.CATheme；这里只负责切换和存到后端 ---------- */

  function themeApi() {
    var T = window.CATheme;
    return T && typeof T.set === 'function' && Array.isArray(T.list) ? T : null;
  }

  /** 换主题：本地立即生效，再存到后端设置 ui.theme（失败不报错）。设置页"外观"也用它。 */
  function setTheme(id, say) {
    var T = themeApi();
    if (!T) { toast('主题还没有装好。', 'info'); return false; }
    T.set(id);
    api('POST', '/api/settings', { ui: { theme: id } }).then(null, function () { /* 本地已经生效 */ });
    if (say) {
      var name = id === 'auto' ? '跟随系统' : id;
      T.list.forEach(function (t) { if (t && t.id === id) name = str(t.name) || id; });
      toast('主题换成了「' + name + '」。', 'info');
    }
    return true;
  }

  /** 启动时：后端设置里的 ui.theme 和本地不同，以后端为准（换个浏览器打开还是同一个主题）。 */
  function syncTheme() {
    var T = themeApi();
    if (!T) return;
    api('GET', '/api/settings').then(function (d) {
      var cfg = d && d.config ? d.config : d;
      var id = cfg && cfg.ui && typeof cfg.ui.theme === 'string' ? cfg.ui.theme : '';
      var cur = '';
      try { cur = str(T.pref()); } catch (e) { cur = ''; }
      if (id && id !== cur) T.set(id);
    }, function () { /* 旧后端或出错：用本地的 */ });
  }

  /* ---------- 小卡片：/状态、/帮助。就地浮在输入框上方，不写进对话记录 ---------- */

  function openSlashCard(which, title, body) {
    var card = $('slash-card');
    if (!card) return;
    closeSlash();
    clear(card);
    card.setAttribute('aria-label', title);
    card.appendChild(h('div', { class: 'slash-card-head' },
      h('span', { class: 'slash-card-title', text: title }),
      h('button', { class: 'toast-x', type: 'button', 'aria-label': '关闭', text: '×', on: { click: function () { closeSlashCard(true); } } })));
    card.appendChild(body);
    card.hidden = false;
    SL.card = which;
    placeSlash(card, which);
  }

  function closeSlashCard(focusBack) {
    var card = $('slash-card');
    if (!card || card.hidden) return false;
    var which = SL.card;
    card.hidden = true;
    clear(card);
    SL.card = null;
    if (focusBack && which) { var i = slashInput(which); if (i) try { i.focus(); } catch (e) { /* 忽略 */ } }
    return true;
  }

  function showStatus(which) {
    var sid = which === 'composer' ? S.sid : null;
    var model = '';
    (S.info.endpoints || []).some(function (e) { if (e && e.model) { model = str(e.model); return true; } return false; });
    var stP = sid
      ? api('GET', '/api/sessions/' + encodeURIComponent(sid) + '/status').then(null, function (e) {
        if (e && (e.missing || e.status === 404)) return { missingStatus: true };
        throw e;
      })
      : api('GET', apiUrl('/api/memories', { workspace: S.wsId })).then(function (d) {
        var m = normalizeMemories(d, S.wsId);
        return { memory: { user: m.usage && m.usage.user, workspace: m.usage && m.usage.workspace } };
      }, function () { return {}; });
    var infoP = api('GET', '/api/state').then(null, function () { return S.info; });
    var capsP = loadCaps().then(null, function () { return S.caps; });
    return Promise.all([stP, infoP, capsP]).then(function (res) {
      var st = res[0] || {}, info = Object.assign({}, res[1] || {});
      if (!info.model) info.model = model;
      if (!sid) { delete st.context; delete st.steps; }
      var rows = statusRows(st, info, function (id) { return modeOf(id).label; }, S.mode);
      var body = h('dl', { class: 'slash-stat' });
      rows.forEach(function (r) {
        body.appendChild(h('dt', { text: r.label }));
        if (r.toolset) { body.appendChild(h('dd', {}, toolsetDetails(r))); return; }
        body.appendChild(h('dd', { class: (r.mono ? 'num' : '') + (r.note ? ' muted' : '') }, r.value,
          r.hint ? h('span', { class: 'stat-hint', text: r.hint }) : null));
      });
      var wrap = h('div', {}, body);
      var feats = featureLines(S.caps);
      if (feats.length) {
        wrap.appendChild(h('div', { class: 'slash-sub', text: '能做的事' }));
        var fdl = h('dl', { class: 'slash-stat feat-stat' });
        feats.forEach(function (f) {
          fdl.appendChild(h('dt', { text: f.name }));
          fdl.appendChild(h('dd', { class: f.ok ? 'is-ok' : 'is-off' },
            h('span', { class: 'feat-mark' + (f.ok ? ' is-ok' : ' is-off'), 'aria-hidden': 'true' }, icon(f.ok ? 'check' : 'x', 12)),
            h('span', { class: 'feat-text' },
              h('span', { class: f.ok ? 'feat-okword' : 'feat-reason', text: f.ok ? '可以用' + (f.via ? '（' + f.via + '）' : '') : f.text }),
              f.ok && f.unknown ? h('span', { class: 'feat-fallback', text: '模型能力还没确认，先按能用处理' }) : null,
              !f.ok && f.fallback ? h('span', { class: 'feat-fallback', text: f.fallback }) : null)));
        });
        wrap.appendChild(fdl);
      }
      if (st.missingStatus) wrap.appendChild(h('p', { class: 'slash-card-note', text: '这个对话的上下文和步数需要更新后端才能看到。' }));
      else if (!sid) wrap.appendChild(h('p', { class: 'slash-card-note', text: '打开一个对话后再看，会多出上下文和步数。' }));
      openSlashCard(which, '状态', wrap);
    });
  }

  /** /状态 里"工具"那一行：点开看带了哪几组、没带哪几组。 */
  function toolsetDetails(r) {
    var ts = r.toolset;
    var det = h('details', { class: 'ts-det' }, h('summary', { text: r.value }));
    function grp(title, ids, on) {
      if (!ids.length) return null;
      return h('div', { class: 'ts-grp' }, h('div', { class: 'ts-cap', text: title }),
        h('div', { class: 'ts-chips' }, ids.map(function (id) {
          var list = ts.groups.length ? ts.groups : TOOLSET_CACHE.list;
          var g = (list || []).filter(function (x) { return x && str(x.id) === id; })[0];
          var tip = [g && g.desc && g.desc !== '常驻' ? str(g.desc) : '', g && Array.isArray(g.tools) ? g.tools.join('、') : ''].filter(Boolean).join('：');
          return h('span', { class: 'tag' + (on ? '' : ' is-off'), title: tip || null, text: toolsetName(id, list) });
        })));
    }
    function fill() {
      while (det.children.length > 1) det.removeChild(det.lastChild);
      append(det, [grp('带着的', ts.loaded, true), grp(ts.lazy ? '没带的（要用时它会自己加载）' : '', ts.unloaded, false),
        ts.chars_all ? h('p', { class: 'stat-hint', text: '全带上是' + approxChars(ts.chars_all) + '。按需加载给上下文小的模型省地方。' }) : null]);
    }
    fill();
    // 组的中文名和每组有哪些工具：GET /api/toolsets（只取一次；没有这个接口就用内置的名字）
    if (!ts.groups.length && !TOOLSET_CACHE.list && !TOOLSET_CACHE.loading) {
      TOOLSET_CACHE.loading = true;
      api('GET', '/api/toolsets').then(function (d) { TOOLSET_CACHE.list = Array.isArray(d) ? d : asList(d, 'toolsets'); fill(); },
        function () { TOOLSET_CACHE.list = []; });
    }
    return det;
  }

  /** 各功能一行：能用的打勾（由别的模型承担时写一句），不能用的写后端的原话，下面小字是"可以改用什么"。 */
  function featureList(lines, cls) {
    var ul = h('ul', { class: cls || 'feat-list' });
    lines.forEach(function (f) {
      var mark = f.ok ? h('span', { class: 'feat-mark is-ok', 'aria-hidden': 'true' }, icon('check', 12))
        : h('span', { class: 'feat-mark is-off', 'aria-hidden': 'true' }, icon('x', 12));
      var note = f.ok ? [f.unknown ? '模型能力还没确认，先按能用处理' : '', f.via].filter(Boolean).join('；') : '';
      ul.appendChild(h('li', { class: 'feat ' + (f.ok ? 'is-ok' : 'is-off') }, mark,
        h('div', { class: 'feat-text' },
          h('span', { class: 'feat-name' }, f.name, h('span', { class: 'sr-only', text: f.ok ? '：可以用' : '：不能用' })),
          note ? h('span', { class: 'feat-note', text: note }) : null,
          !f.ok ? h('span', { class: 'feat-reason', text: f.text }) : null,
          !f.ok && f.fallback ? h('span', { class: 'feat-fallback', text: f.fallback }) : null)));
    });
    return ul;
  }

  function showHelp(which) {
    var list = h('div', { class: 'slash-help', role: 'list' });
    var ctx = slashCtx(which);
    SLASH_COMMANDS.forEach(function (c) {
      // 别的模式（或老后端 / 新后端）才有的指令不列
      if (Array.isArray(c.space) && c.space.indexOf(str(ctx.mode)) < 0) return;
      var ok = slashAvailable(c, ctx);
      list.appendChild(h('button', { class: 'slash-help-row' + (ok ? '' : ' is-off'), type: 'button', role: 'listitem',
        title: ok ? '放进输入框' : availText(c, ctx),
        on: { click: function () {
          closeSlashCard(false);
          SL.off[which] = false;
          setInput(which, '/' + c.name + (c.arg ? ' ' : ''));
          slashUpdate(which);
        } } },
        h('span', { class: 'slash-cmd', text: '/' + c.name }),
        h('span', { class: 'slash-alias', text: '/' + c.alias }),
        h('span', { class: 'slash-desc', text: c.desc + (c.arg ? '  ' + c.arg : '') })));
    });
    var ns = (SL.skills || []).length, nt = 0;
    (S.templates || []).forEach(function (g) { nt += (g.items || []).length; });
    var foot = h('p', { class: 'slash-card-note', text: '每个技能（' + ns + ' 个）和模板（' + nt + ' 个）也是一条指令，打 / 在菜单里能看到。灰色的指令现在用不了。' });
    openSlashCard(which, '全部指令', h('div', {}, list, foot));
  }

  /* ---------------- 语音输入 ----------------
   * 点一下开始录音，再点一下结束并识别；Esc 取消。识别出的文字插入到输入框光标处，不自动发送。
   * 录音：getUserMedia + Web Audio 采集浮点样本（优先 AudioWorklet，不可用或收不到数据时换 ScriptProcessor），
   * 结束后在前端重采样到 16 kHz 单声道 16 位，编码成 WAV 发给 /api/transcribe。
   */

  var V = {
    state: 'idle',        // idle · starting 正在打开麦克风 · recording · transcribing
    which: null,          // 'home' 或 'composer'
    seq: 0,               // 每次开始、取消都加一；过期的回调据此丢弃
    ctx: null, stream: null, src: null, node: null, sink: null, kind: '',
    chunks: [], frames: 0, rate: 0, peak: 0, rms: 0,
    t0: 0, timer: null, watchdog: null, abort: null, startSid: null
  };

  var WORKLET_SRC = [
    'class CaCapture extends AudioWorkletProcessor {',
    '  constructor() { super(); this.buf = new Float32Array(2048); this.n = 0; }',
    '  process(inputs) {',
    '    var inp = inputs[0];',
    '    if (inp && inp.length && inp[0]) {',
    '      var len = inp[0].length, chs = inp.length;',
    '      for (var i = 0; i < len; i++) {',
    '        var v = 0;',
    '        for (var c = 0; c < chs; c++) v += inp[c][i];',
    '        this.buf[this.n++] = v / chs;',
    '        if (this.n === this.buf.length) { this.port.postMessage(this.buf); this.buf = new Float32Array(2048); this.n = 0; }',
    '      }',
    '    }',
    '    return true;',
    '  }',
    '}',
    "registerProcessor('ca-capture', CaCapture);"
  ].join('\n');

  function maxVoiceSeconds() {
    var m = Number(S.info && S.info.asr_max_seconds);
    return m > 0 ? m : 180;
  }

  function voiceTarget() {
    return S.route === 'home' ? 'home' : S.route === 'task' ? 'composer' : null;
  }

  function voiceButtons() {
    return { home: { btn: $('home-mic'), st: $('home-voice') }, composer: { btn: $('composer-mic'), st: $('composer-voice') } };
  }

  /** 按当前状态更新两个麦克风按钮和旁边的状态。 */
  function renderVoice() {
    var all = voiceButtons(), mac = isMac();
    var tip = '语音输入（' + (mac ? '⌘' : 'Ctrl') + ' + Shift + M）';
    ['home', 'composer'].forEach(function (w) {
      var b = all[w].btn, st = all[w].st;
      if (!b) return;
      var mine = V.which === w ? V.state : 'idle';
      b.classList.toggle('is-recording', mine === 'recording' || mine === 'starting');
      b.classList.toggle('is-busy', mine === 'transcribing');
      b.disabled = mine === 'transcribing' || (V.state !== 'idle' && V.which !== w);
      b.setAttribute('aria-pressed', mine === 'recording' || mine === 'starting' ? 'true' : 'false');
      var label = mine === 'recording' ? '停止录音并识别（Esc 取消）' : mine === 'starting' ? '正在打开麦克风，点一下取消'
        : mine === 'transcribing' ? '正在识别' : tip;
      b.setAttribute('aria-label', mine === 'idle' ? '语音输入' : label);
      b.title = label;
      if (!st) return;
      st.hidden = mine === 'idle';
      st.classList.toggle('is-recording', mine === 'recording');
      var time = st.querySelector('.voice-time'), meter = st.querySelector('.voice-meter'), msg = st.querySelector('.voice-msg');
      if (mine === 'recording') {
        setText(time, formatDuration((Date.now() - V.t0) / 1000));
        show(time, true); show(meter, true); setText(msg, '');
        var fill = meter && meter.firstChild;
        if (fill && fill.style) fill.style.transform = 'scaleX(' + meterLevel(V.rms).toFixed(3) + ')';
      } else {
        show(time, false); show(meter, false);
        setText(msg, mine === 'starting' ? '正在打开麦克风…' : mine === 'transcribing' ? '正在识别…' : '');
      }
    });
  }

  function isMac() { return /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || ''); }

  function toggleVoice(which) {
    which = which || voiceTarget();
    if (V.state === 'recording') return stopVoice();
    if (V.state === 'starting') { cancelVoice(true); return; }
    if (V.state === 'transcribing') { toast('正在识别，请稍等。', 'info'); return; }
    if (!which) { toast('在对话页才能语音输入。', 'info'); return; }
    return startVoice(which);
  }

  function voiceFail(kind, detail) {
    var e = voiceError(kind, detail);
    toast(e.text, 'error', e.settings ? { label: '去设置', run: function () { go('#/settings'); } } : null);
  }

  function startVoice(which) {
    if (V.state !== 'idle') return;
    if (S.info && S.info.asr_engine === 'off') { voiceFail('not-configured', '语音输入还没有开启。请到「设置 → 语音识别」里选择识别方式。'); return; }
    var AC = window.AudioContext || window.webkitAudioContext;
    var md = navigator.mediaDevices;
    if (!AC || !md || typeof md.getUserMedia !== 'function') { voiceFail('unsupported'); return; }
    var ctx;
    // 在点击的同一个调用里建 AudioContext 并 resume：Safari 只在用户操作里允许它开始运行
    try { ctx = new AC(); } catch (e) { voiceFail('unsupported'); return; }
    try { if (ctx.resume) ctx.resume().then(null, function () {}); } catch (e) { /* 忽略 */ }
    var my = ++V.seq;
    V.state = 'starting'; V.which = which; V.ctx = ctx; V.chunks = []; V.frames = 0; V.peak = 0; V.rms = 0;
    V.rate = ctx.sampleRate; V.startSid = S.sid;
    renderVoice();
    var p;
    try { p = md.getUserMedia({ audio: true }); } catch (e) { p = Promise.reject(e); }
    return Promise.resolve(p).then(function (stream) {
      if (my !== V.seq) { stopTracks(stream); return; }
      V.stream = stream;
      var tracks = stream.getAudioTracks ? stream.getAudioTracks() : [];
      tracks.forEach(function (t) {
        t.onended = function () { if (my === V.seq && V.state === 'recording') { toast('麦克风断开了，已用录到的部分识别。', 'info'); stopVoice(); } };
      });
      return attachCapture(ctx, stream, my).then(function () {
        if (my !== V.seq) return;
        V.state = 'recording';
        V.t0 = Date.now();
        V.timer = setInterval(guard(voiceTick), 100);
        renderVoice();
      });
    }).then(null, function (e) {
      if (my !== V.seq) return;
      resetVoice();
      voiceFail(e && e.name ? micErrorKind(e.name) : 'mic-failed');
    });
  }

  function onChunk(my, data) {
    if (my !== V.seq || (V.state !== 'recording' && V.state !== 'starting') || !data || !data.length) return;
    V.chunks.push(data);
    V.frames += data.length;
    var sum = 0, pk = V.peak;
    for (var i = 0; i < data.length; i++) { var x = data[i]; sum += x * x; if (x > pk) pk = x; else if (-x > pk) pk = -x; }
    V.peak = pk;
    V.rms = Math.sqrt(sum / data.length);
  }

  /** 接上采集节点。先试 AudioWorklet，失败就用 ScriptProcessor。 */
  function attachCapture(ctx, stream, my) {
    var src = ctx.createMediaStreamSource(stream);
    var sink = ctx.createGain();
    sink.gain.value = 0;                       // 节点要连到输出才会被驱动；音量设成 0，不会从扬声器放出来
    sink.connect(ctx.destination);
    V.src = src; V.sink = sink;
    return useWorklet(ctx, src, sink, my).then(function () {
      // 保险：一秒半后还没收到任何数据，换成 ScriptProcessor
      V.watchdog = setTimeout(function () {
        V.watchdog = null;
        if (my !== V.seq || V.state !== 'recording' || V.frames > 0) return;
        try { if (ctx.state === 'suspended' && ctx.resume) ctx.resume().then(null, function () {}); } catch (e) { /* 忽略 */ }
        disconnectNode();
        try { useScriptProcessor(ctx, src, sink, my); } catch (e) { cancelVoice(true); voiceFail('mic-failed'); }
      }, 1500);
    }, function () {
      useScriptProcessor(ctx, src, sink, my);
    });
  }

  function useWorklet(ctx, src, sink, my) {
    if (!ctx.audioWorklet || typeof window.AudioWorkletNode !== 'function' || !window.Blob || !window.URL) return Promise.reject(new Error('no worklet'));
    var url;
    try { url = URL.createObjectURL(new Blob([WORKLET_SRC], { type: 'application/javascript' })); } catch (e) { return Promise.reject(e); }
    return ctx.audioWorklet.addModule(url).then(function () {
      try { URL.revokeObjectURL(url); } catch (e) { /* 忽略 */ }
      if (my !== V.seq) return;
      var node = new window.AudioWorkletNode(ctx, 'ca-capture', { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1] });
      node.port.onmessage = function (ev) { onChunk(my, ev.data); };
      src.connect(node);
      node.connect(sink);
      V.node = node; V.kind = 'worklet';
    }, function (e) {
      try { URL.revokeObjectURL(url); } catch (x) { /* 忽略 */ }
      throw e;
    });
  }

  function useScriptProcessor(ctx, src, sink, my) {
    if (my !== V.seq) return;
    var fn = ctx.createScriptProcessor || ctx.createJavaScriptNode;
    if (!fn) throw new Error('这个浏览器不能录音。');
    var node = fn.call(ctx, 4096, 1, 1);
    node.onaudioprocess = function (ev) { onChunk(my, new Float32Array(ev.inputBuffer.getChannelData(0))); };
    src.connect(node);
    node.connect(sink);
    V.node = node; V.kind = 'script';
  }

  function disconnectNode() {
    var n = V.node;
    V.node = null;
    if (!n) return;
    try { if (n.port) n.port.onmessage = null; } catch (e) { /* 忽略 */ }
    try { n.onaudioprocess = null; } catch (e) { /* 忽略 */ }
    try { n.disconnect(); } catch (e) { /* 忽略 */ }
    try { if (V.src) V.src.disconnect(n); } catch (e) { /* 忽略 */ }
  }

  function stopTracks(stream) {
    try { (stream.getTracks ? stream.getTracks() : []).forEach(function (t) { t.onended = null; t.stop(); }); } catch (e) { /* 忽略 */ }
  }

  /** 关掉麦克风和音频图。不改 state。 */
  function releaseAudio() {
    if (V.timer) { clearInterval(V.timer); V.timer = null; }
    if (V.watchdog) { clearTimeout(V.watchdog); V.watchdog = null; }
    disconnectNode();
    try { if (V.src) V.src.disconnect(); } catch (e) { /* 忽略 */ }
    try { if (V.sink) V.sink.disconnect(); } catch (e) { /* 忽略 */ }
    if (V.stream) stopTracks(V.stream);
    if (V.ctx && V.ctx.close) { try { V.ctx.close().then(null, function () {}); } catch (e) { /* 忽略 */ } }
    V.src = V.sink = V.stream = V.ctx = null;
  }

  function resetVoice() {
    releaseAudio();
    if (V.abort) { try { V.abort.abort(); } catch (e) { /* 忽略 */ } }
    V.abort = null;
    V.state = 'idle';
    V.chunks = [];
    V.frames = 0;
    renderVoice();
  }

  /** 取消本次录音或识别，什么都不放进输入框。 */
  function cancelVoice(quiet) {
    if (V.state === 'idle') return false;
    V.seq++;
    resetVoice();
    if (!quiet) toast('已取消语音输入。', 'info');
    return true;
  }

  function voiceTick() {
    if (V.state !== 'recording') return;
    var el = (Date.now() - V.t0) / 1000;
    if (el >= maxVoiceSeconds()) {
      toast('一次最多录 ' + formatDuration(maxVoiceSeconds()) + '，已自动结束并开始识别。', 'info');
      stopVoice();
      return;
    }
    renderVoice();
  }

  /** 结束录音并识别。 */
  function stopVoice() {
    if (V.state === 'starting') { cancelVoice(true); return; }
    if (V.state !== 'recording') return;
    var which = V.which, rate = V.rate, peak = V.peak;
    var pcm = concatSamples(V.chunks, Math.floor(maxVoiceSeconds() * rate));
    releaseAudio();
    V.chunks = [];
    if (!pcm.length) { resetVoice(); voiceFail('silent'); return; }
    if (pcm.length / rate < 0.4) { resetVoice(); toast('说话时间太短，没有识别。', 'info'); return; }
    if (peak < SILENCE_PEAK) { resetVoice(); voiceFail('silent'); return; }
    var wav = encodeWav(resample(pcm, rate, VOICE_RATE), VOICE_RATE);
    pcm = null;
    var my = V.seq;
    V.state = 'transcribing';
    renderVoice();
    var ctl = typeof window.AbortController === 'function' ? new window.AbortController() : null;
    V.abort = ctl;
    var timedOut = false;
    var timer = setTimeout(function () { timedOut = true; if (ctl) ctl.abort(); }, 150000);
    var opt = { method: 'POST', headers: { 'X-Token': S.token, 'Content-Type': 'audio/wav' }, body: new Blob([wav], { type: 'audio/wav' }), cache: 'no-store' };
    if (ctl) opt.signal = ctl.signal;
    return fetch('/api/transcribe', opt).then(function (r) {
      return r.text().then(function (txt) {
        var data = null;
        try { data = txt ? JSON.parse(txt) : null; } catch (e) { data = null; }
        if (!r.ok) { var err = new Error('transcribe'); err.status = r.status; err.detail = data && data.error; throw err; }
        return data;
      });
    }).then(function (d) {
      clearTimeout(timer);
      if (my !== V.seq) return;
      V.abort = null;
      resetVoice();
      var text = str(d && d.text).trim();
      if (!text) { voiceFail('empty'); return; }
      putVoiceText(which, text);
    }, function (e) {
      clearTimeout(timer);
      if (my !== V.seq) return;
      V.abort = null;
      resetVoice();
      if (timedOut) voiceFail('timeout');
      else voiceFail(e && e.status ? httpErrorKind(e.status) : 'offline', e && e.detail);
    });
  }

  /** 把识别结果放进输入框的光标处。用户已经离开原来的页面时不放，改为提示并可复制。 */
  function putVoiceText(which, text) {
    var c = composerFor(which), input = c.input;
    var moved = which === 'home' ? S.route !== 'home' : (S.route !== 'task' || S.sid !== V.startSid);
    if (!input || moved) {
      toast('识别好了，但你已经离开了原来的页面，文字没有放进输入框：' + clip(text, 60), 'info',
        navigator.clipboard && navigator.clipboard.writeText ? { label: '复制文字', run: function () {
          return navigator.clipboard.writeText(text).then(function () { toast('已复制。', 'info'); });
        } } : null);
      return;
    }
    var r = insertAtCursor(input.value, input.selectionStart, input.selectionEnd, text);
    input.value = r.value;     // 只写纯文本到 value
    try { input.focus(); input.setSelectionRange(r.caret, r.caret); } catch (e) { /* 忽略 */ }
    if (which !== 'home') autosize(input);
    updateSendBtn(which);
  }

  /* ---------------- 批准方式 ---------------- */

  function modeOf(id) {
    for (var i = 0; i < MODES.length; i++) if (MODES[i].id === id) return MODES[i];
    return MODES[1];
  }

  function renderModeButtons() {
    var m = modeOf(S.info.approval_mode);
    ['home-mode', 'composer-mode'].forEach(function (id) {
      var b = $(id);
      if (!b) return;
      clear(b);
      append(b, [h('span', { class: 'mode-caption', text: '批准：' }), h('span', { class: 'mode-name', text: m.label })]);
      b.title = m.desc + '（点这里修改）';
    });
  }

  function openModeMenu(btn) {
    openPop('mode-menu', btn, function (menu) {
      menu.appendChild(h('div', { class: 'menu-caption', text: 'Agent 动手之前，要不要先问你？' }));
      MODES.forEach(function (m) {
        var cur = m.id === (S.info.approval_mode || 'auto');
        menu.appendChild(h('button', { class: 'menu-item' + (cur ? ' is-current' : ''), type: 'button', role: 'menuitemradio',
          'aria-checked': cur ? 'true' : 'false', on: { click: function () { closeMenus(true); return setMode(m.id); } } },
          h('span', { class: 'mi-label', text: m.label }), h('span', { class: 'mi-desc', text: m.desc })));
      });
      menu.appendChild(h('div', { class: 'menu-foot', text: '删除文件不管选哪种都会先问你。' }));
    });
    var cur = $('mode-menu') && !$('mode-menu').hidden ? $('mode-menu').querySelector('.is-current') : null;
    if (cur) cur.focus();
  }

  function setMode(id) {
    var old = S.info.approval_mode;
    S.info.approval_mode = id;
    renderModeButtons();
    return api('POST', '/api/settings', { approval_mode: id }).then(function () {
      toast('批准方式改成了「' + modeOf(id).label + '」。', 'info');
    }, function (e) { S.info.approval_mode = old; renderModeButtons(); throw e; });
  }

  /* ---------------- 两种工作模式：总开关、导航、新对话页 ---------------- */

  function modeName(id) { var m = findModeById(S.modes, id); return m ? m.name : str(id); }

  /** 切到另一个模式（不跳页面；跳页面由 route 管）：对话列表、搜索、建议、导航都换成那个模式的。 */
  function applyMode(id) {
    if (!findModeById(S.modes, id) || id === S.mode) return;
    S.mode = id;
    try { localStorage.setItem('ca.mode', id); } catch (e) { /* 忽略 */ }
    resetSearch(true);
    S.listLoading = true;
    S.templates = S.tplBy[id] || [];
    S.tplMore = false;
    S.deep.home = false;
    renderModeSwitch();
    renderNav();
    renderTaskList();
    loadSessions().then(null, report);
    loadTemplates();
    if (id === 'hspace' && !S.libs) loadLibraries();
  }

  /** 用户点了总开关：去那个模式的新对话页。 */
  function switchMode(id) {
    // 先换模式再跳：go() 会给没写模式的地址补上"当前模式"，而 CariAi 的地址不带前缀，
    // 不先换的话，从 Hspace 点 CariAi 会被补回 #/hspace，等于没切
    applyMode(id);
    go(buildHash(id, 'new'));
  }

  function renderModeSwitch() {
    var box = $('mode-switch');
    if (!box) return;
    document.body.classList.toggle('has-modes', S.modes.length > 0);
    if (S.mode) document.body.setAttribute('data-workmode', S.mode); else document.body.removeAttribute('data-workmode');
    show(box, S.modes.length > 0);
    if (!S.modes.length) return;
    var focused = box.contains(document.activeElement);
    clear(box);
    S.modes.forEach(function (m, i) {
      var sel = m.id === S.mode;
      box.appendChild(h('button', { class: 'ms-tab' + (sel ? ' is-on' : ''), type: 'button', role: 'tab', id: 'ms-' + m.id,
        'aria-selected': sel ? 'true' : 'false', tabindex: sel ? '0' : '-1', 'data-mode': m.id,
        title: m.tagline ? m.name + '：' + m.tagline : m.name,
        on: {
          click: function () { switchMode(m.id); },
          keydown: function (e) {
            var n = S.modes.length, j = -1;
            if (e.key === 'ArrowRight' || e.key === 'ArrowDown') j = (i + 1) % n;
            else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') j = (i + n - 1) % n;
            else if (e.key === 'Home') j = 0;
            else if (e.key === 'End') j = n - 1;
            if (j < 0) return;
            e.preventDefault();
            switchMode(S.modes[j].id);
            var b = $('ms-' + S.modes[j].id);
            if (b) b.focus();
          }
        } },
        h('span', { class: 'ms-name', text: m.name }),
        h('span', { class: 'ms-short', 'aria-hidden': 'true', text: m.name.charAt(0) })));
    });
    if (focused) { var cur = $('ms-' + S.mode); if (cur) cur.focus(); }
  }

  var PAGE_KB_OLD = PAGES.kb;
  var PAGE_KB_NEW = { title: '知识库', sub: '资料库收的是电脑上或共享盘里的文件夹。程序只读不改；删掉资料库只删索引，不动文件。' };

  /** 侧栏导航：两个模式一样，Hspace 里"知识库"排第一。老后端照旧（"资料"排第一）。 */
  function renderNav() {
    var nav = $('nav'), pal = $('open-palette');
    if (!nav) return;
    PAGES.kb = S.modes.length ? PAGE_KB_NEW : PAGE_KB_OLD;
    var items = navItems(S.mode);
    var btns = nav.querySelectorAll('.nav-btn[data-view]');
    for (var i = 0; i < btns.length; i++) btns[i].hidden = items.indexOf(btns[i].getAttribute('data-view')) < 0;
    items.forEach(function (v) {
      var b = nav.querySelector('.nav-btn[data-view="' + v + '"]');
      if (b && pal) nav.insertBefore(b, pal);
    });
    var kb = nav.querySelector('.nav-btn[data-view="kb"]');
    if (kb) { var t = kb.querySelector('.nav-text'); setText(t, PAGES.kb.title); kb.title = PAGES.kb.title; }
  }

  /* ---------------- 资料库（Hspace 的底子） ---------------- */

  var libsReq = null;
  function loadLibraries() {
    if (!S.modes.length) return Promise.resolve([]);
    if (libsReq) return libsReq;
    libsReq = api('GET', '/api/libraries').then(function (d) {
      S.libs = normalizeLibraries(d);
      S.libsMissing = false;
    }, function (e) {
      if (e && (e.missing || e.status === 404 || e.status === 405)) { S.libs = []; S.libsMissing = true; }
      else if (!S.libs) S.libs = [];
    }).then(function () {
      libsReq = null;
      S.libsAt = Date.now();
      var ids = S.libs.map(function (l) { return l.id; });
      S.scope = S.scope.filter(function (x) { return ids.indexOf(x) >= 0; });
      if (S.route === 'home') renderHomeKb();
      return S.libs;
    });
    return libsReq;
  }

  /** 收录进度事件：资料库列表、新对话页的状态行、知识库页都跟着变。 */
  function onKbProgress(p) {
    if (!p || typeof p !== 'object' || !p.library_id) return;
    S.lastKbEvent = Date.now();
    if (Array.isArray(S.libs)) S.libs = S.libs.map(function (l) { return applyKbProgress(l, p); });
    if (S.kbLive) { try { S.kbLive(p); } catch (e) { if (window.console) console.error(e); } }
    if (S.route === 'home' && S.mode === 'hspace') renderHomeKb();
  }

  /** 文档卡片的进度事件（第 1.5 节，不落账）：资料库上的"摘要 180 / 212"跟着变。 */
  function onKbCards(p) {
    if (!p || typeof p !== 'object' || !p.library_id) return;
    if (Array.isArray(S.libs)) S.libs = S.libs.map(function (l) { return applyKbCards(l, p); });
    if (S.kbLive) { try { S.kbLive(Object.assign({ __cards: true }, p)); } catch (e) { if (window.console) console.error(e); } }
  }

  /** Hspace 新对话页输入框下面：范围一行 + 状态一行；一份资料都没有时是一句话加一个按钮。 */
  function renderHomeKb() {
    var box = $('home-kb');
    if (!box) return;
    var on = S.mode === 'hspace' && !S.libsMissing;
    show(box, on);
    if (!on) return;
    clear(box);
    if (!S.libs) { loadLibraries(); return; }
    if (!S.libs.length) {
      box.classList.add('is-empty');
      box.appendChild(h('p', { class: 'hk-empty' },
        h('span', { text: '还没有资料。' }),
        h('button', { class: 'hk-add', type: 'button', disabled: S.picking, on: { click: function () { return newLibraryFlow(''); } } },
          icon(IC_FOLDER, 14), h('span', { text: S.picking ? '正在等你选择文件夹…' : '添加一个放 SOP 的文件夹' }))));
      return;
    }
    box.classList.remove('is-empty');
    var st = kbStatusText(S.libs, gate('semantic_search'));
    box.appendChild(h('div', { class: 'hk-row' },
      h('button', { class: 'scope-btn', type: 'button', 'aria-haspopup': 'menu', 'aria-expanded': 'false',
        title: '选这次查哪些资料库',
        on: { click: function (e) { openScopeMenu(e.currentTarget, 'home'); } } },
        icon('book', 14), h('span', { class: 'scope-label', text: '查：' }), h('span', { class: 'scope-val', text: scopeLabel(scopeLibs(), getScope('home'), S.docNames) }),
        icon('down', 12))));
    box.appendChild(h('p', { class: 'hk-status', title: st.reason || null },
      h('span', { text: st.head }), st.busy ? h('span', { class: 'hk-tail', text: st.busy }) : null,
      st.tail ? h('span', { class: 'hk-tail', text: st.tail }) : null));
  }

  /* ---------- 检索范围：资料库、标签、指定文件（第 2.4、7.2 节） ----------
   * home：新对话页（下一个对话的范围，存在 S.scope / S.scopeTags / S.scopeDocs）；
   * composer：当前对话（S.view.libraries / tags / docs，改了立刻 PATCH 到后端）。 */

  function scopeTarget(which) { return which === 'composer' && S.sid && S.view ? 'session' : 'home'; }

  function getScope(which) {
    if (scopeTarget(which) === 'session') return normScope({ libraries: S.view.libraries, docs: S.view.docs, tags: S.view.tags });
    return normScope({ libraries: S.scope, docs: S.scopeDocs, tags: S.scopeTags });
  }

  /** 改范围。对话里的改动发给后端，失败就退回去。 */
  function setScope(which, next, quiet) {
    next = normScope(next);
    if (scopeTarget(which) === 'session') {
      var v = S.view, sid = v.sid, old = getScope(which), body = {};
      ['libraries', 'docs', 'tags'].forEach(function (k) { if (old[k].join('\n') !== next[k].join('\n')) body[k] = next[k]; });
      if (!Object.keys(body).length) return Promise.resolve();
      v.libraries = next.libraries; v.docs = next.docs; v.tags = next.tags;
      renderScopeChips('composer');
      return api('PATCH', '/api/sessions/' + encodeURIComponent(sid), body).then(function () {
        if (!quiet) toast('这个对话接下来查：' + scopeLabel(S.libs || [], next, S.docNames) + '。', 'info');
      }, function (e) {
        if (S.view && S.view.sid === sid) { S.view.libraries = old.libraries; S.view.docs = old.docs; S.view.tags = old.tags; renderScopeChips('composer'); }
        throw e;
      });
    }
    S.scope = next.libraries; S.scopeDocs = next.docs; S.scopeTags = next.tags;
    renderHomeKb();
    renderScopeChips('home');
    return Promise.resolve();
  }

  /** 后端有没有标签、指定文件这一套（资料库列表里带 cards / tags 字段）。 */
  function scopeV2() { return (S.libs || []).some(function (l) { return l.v2; }); }

  /** 范围菜单里能选的资料库：停用了的不算。 */
  function scopeLibs() { return (S.libs || []).filter(function (l) { return l.enabled !== false; }); }

  /** 输入框上方的小标签：选了哪些资料库（没选全部时）、标签、文件；每个可以点 × 去掉。 */
  function renderScopeChips(which) {
    var box = $(which + '-scope');
    if (!box) return;
    clear(box);
    var on = S.mode === 'hspace' && !S.libsMissing && (which === 'home' || (S.view && (S.view.mode === 'hspace' || !S.view.mode) && S.sid));
    // 对话页里资料库列表可能还没取过：取到了再画一次
    if (on && !S.libs) { loadLibraries().then(function () { if (S.libs) renderScopeChips(which); }); }
    var chips = on ? scopeChips(scopeLibs(), getScope(which), S.docNames) : [];
    // 对话里存的只有文档编号：名字还不知道时取一次文档列表，取到了再画
    var unknown = on ? getScope(which).docs.filter(function (id) { return !S.docNames[id] && !DOCS_CACHE.tried[id]; }) : [];
    if (unknown.length && !S.docNamesLoading && scopeV2()) {
      unknown.forEach(function (id) { DOCS_CACHE.tried[id] = true; });
      S.docNamesLoading = true;
      loadAllDocs().then(function () { S.docNamesLoading = false; renderScopeChips(which); }, function () { S.docNamesLoading = false; });
    }
    box.hidden = !chips.length;
    if (!chips.length) return;
    box.appendChild(h('span', { class: 'sc-label', text: '只查' }));
    chips.forEach(function (c) {
      box.appendChild(h('span', { class: 'sc-chip sc-' + c.kind, title: c.kind === 'doc' ? '只在这份文件里找' : c.kind === 'tag' ? '只在带这个标签的文件里找' : '只查这个资料库' },
        c.kind === 'doc' ? icon(IC_FILE, 12) : c.kind === 'lib' ? icon('book', 12) : null,
        h('span', { class: 'sc-text', text: c.kind === 'doc' ? fileName(c.label) : c.label }),
        h('button', { class: 'sc-x', type: 'button', 'aria-label': '不再限定：' + c.label, title: '去掉', text: '×', on: { click: function () {
          var cur = getScope(which);
          if (c.kind === 'lib') cur.libraries = cur.libraries.filter(function (x) { return x !== c.id; });
          if (c.kind === 'tag') cur.tags = cur.tags.filter(function (x) { return x !== c.id; });
          if (c.kind === 'doc') cur.docs = cur.docs.filter(function (x) { return x !== c.id; });
          var inp = composerFor(which).input;
          return Promise.resolve(setScope(which, cur, true)).then(function () { if (inp) inp.focus(); });
        } } })));
    });
  }

  /** "查哪些资料库"的小菜单：带勾选，点了不关，至少留一个库。新后端多两样：按标签、指定文件…。
   *  target：home（新对话页）或 session（当前对话）。 */
  function openScopeMenu(btn, target) {
    var which = target === 'session' ? 'composer' : 'home';
    var libs = scopeLibs();
    if (!libs.length) { go('#/kb'); return; }
    function build(menu) {
      clear(menu);
      var sc = getScope(which), sel = sc.libraries.filter(function (id) { return libs.some(function (l) { return l.id === id; }); });
      var all = !sel.length || sel.length === libs.length;
      menu.appendChild(h('div', { class: 'menu-caption', text: target === 'session' ? '这个对话查哪些资料库' : '这次查哪些资料库' }));
      menu.appendChild(scopeItem('全部资料库', '', all, function () { sc.libraries = []; return setScope(which, sc); }, 'menuitemradio'));
      menu.appendChild(h('div', { class: 'menu-sep' }));
      libs.forEach(function (l) {
        var on = all || sel.indexOf(l.id) >= 0;
        menu.appendChild(scopeItem(l.name, groupNum(l.docs) + ' 份' + (l.shared ? ' · 公司' : '') + (l.state === 'indexing' ? ' · 正在收录' : ''), on, function () {
          sc.libraries = toggleScope(libs, sel, l.id);
          return setScope(which, sc);
        }, 'menuitemcheckbox'));
      });
      if (scopeV2()) {
        menu.appendChild(h('div', { class: 'menu-sep' }));
        menu.appendChild(menuItem('指定文件…', sc.docs.length ? '已选 ' + sc.docs.length + ' 份。也可以在输入框里打 #' : '只在某几份文件里找。也可以在输入框里打 #',
          function () { setTimeout(function () { openDocPicker(btn, which); }, 0); }));
        var inLibs = all ? libs : libs.filter(function (l) { return sel.indexOf(l.id) >= 0; });
        var tags = mergeTags(inLibs).slice(0, 16);
        sc.tags.forEach(function (t) { if (!tags.some(function (x) { return x.tag === t; })) tags.push({ tag: t, docs: 0 }); });
        if (tags.length) {
          menu.appendChild(h('div', { class: 'menu-sep' }));
          menu.appendChild(h('div', { class: 'menu-caption', text: '按标签：只查带这些标签的文件' }));
          // 标签排成一行行的小块（仍然是菜单项，上下键能走到）
          menu.appendChild(h('div', { class: 'scope-tags', role: 'group', 'aria-label': '按标签' }, tags.map(function (t) {
            var on = sc.tags.indexOf(t.tag) >= 0;
            return h('button', { class: 'menu-item scope-tag' + (on ? ' is-checked' : ''), type: 'button', role: 'menuitemcheckbox',
              'aria-checked': on ? 'true' : 'false', title: t.docs ? t.docs + ' 份文件带这个标签' : null,
              on: { click: function (e) { return keepFocus(e, function () { sc.tags = toggleIn(sc.tags, t.tag); return setScope(which, sc); }); } } },
              on ? icon('check', 11) : null, h('span', { text: t.tag }), t.docs ? h('span', { class: 'chip-n', text: String(t.docs) }) : null);
          })));
        }
      }
      menu.appendChild(h('div', { class: 'menu-sep' }));
      menu.appendChild(menuItem('管理知识库…', '', function () { go('#/kb'); }));
    }
    /** 点了一项：改完重画菜单，焦点留在同一项上。 */
    function keepFocus(e, run) {
      var menu = $('scope-menu'), items = Array.prototype.slice.call(menu.querySelectorAll('.menu-item'));
      var idx = items.indexOf(e.currentTarget);
      return Promise.resolve(run()).then(function () {
        if (menu.hidden) return;
        build(menu);
        var again = menu.querySelectorAll('.menu-item')[idx];
        if (again) again.focus();
      });
    }
    function scopeItem(label, desc, checked, run, role) {
      return h('button', { class: 'menu-item scope-item' + (checked ? ' is-checked' : ''), type: 'button', role: role,
        'aria-checked': checked ? 'true' : 'false',
        on: { click: function (e) { return keepFocus(e, run); } } },
        h('span', { class: 'scope-check', 'aria-hidden': 'true' }, checked ? icon('check', 12) : null),
        h('span', { class: 'mi-label', text: label }), desc ? h('span', { class: 'mi-desc', text: desc }) : null);
    }
    openPop('scope-menu', btn, build);
  }

  /** 所有启用的资料库里的文档（每个库一分钟内用缓存）。"#"和"指定文件…"用。 */
  function loadAllDocs() {
    var libs = scopeLibs();
    return Promise.all(libs.map(function (l) {
      var c = DOCS_CACHE.byLib[l.id];
      if (c && Date.now() - c.at < 60000) return Promise.resolve(c.docs.map(function (d) { return Object.assign({ library: l.name }, d); }));
      return api('GET', '/api/libraries/' + encodeURIComponent(l.id) + '/docs').then(function (d) {
        var docs = asList(d, 'docs').map(normDoc).filter(Boolean);
        DOCS_CACHE.byLib[l.id] = { at: Date.now(), docs: docs };
        docs.forEach(function (x) { if (x.id) S.docNames[x.id] = x.title || fileName(x.path); });
        return docs.map(function (x) { return Object.assign({ library: l.name }, x); });
      }, function () { return []; });
    })).then(function (lists) { return [].concat.apply([], lists); });
  }

  /** 按文件名、摘要、关键词、标签找文档。新后端用 GET /api/kb/docs?q=（它会看关键词）；没有这个接口或没写字时在本地筛。
   *  all 是 loadAllDocs() 的结果（本地筛、补库名用）。停用了的库里的文件不列。 */
  function findDocs(q, limit, all) {
    var k = str(q).trim();
    var local = function () { return searchDocs(all || [], k, limit); };
    if (!k || DOCS_CACHE.remoteMissing) return Promise.resolve(local());
    var on = {};
    scopeLibs().forEach(function (l) { on[l.id] = l.name; });
    return api('GET', apiUrl('/api/kb/docs', { q: k })).then(function (d) {
      return asList(d, 'docs').map(normDoc).filter(function (x) { return x && x.id && (!x.library_id || on[x.library_id] != null); })
        .map(function (x) { if (!x.library && x.library_id) x.library = on[x.library_id]; S.docNames[x.id] = x.title || fileName(x.path); return x; })
        .slice(0, limit || 30);
    }, function (e) {
      if (e && (e.missing || e.status === 404 || e.status === 405)) DOCS_CACHE.remoteMissing = true;
      return local();
    });
  }

  /** "指定文件…"：一个搜索框，按文件名和摘要找，可以选几份。勾了立刻生效，Esc 或"好了"关上。 */
  function openDocPicker(btn, which) {
    var anchor = btn && document.contains(btn) ? btn : composerFor(which).input;
    openPop('doc-pop', anchor, function (m) {
      var q = h('input', { type: 'search', class: 'dp-q', placeholder: '按文件名或摘要找', 'aria-label': '按文件名或摘要找文件' });
      var list = h('div', { class: 'dp-list', role: 'group', 'aria-label': '文件' }, h('p', { class: 'muted small dp-empty', text: '正在读取…' }));
      var docs = [], found = null, fseq = 0;
      function search() {
        var me = ++fseq;
        return findDocs(q.value, 40, docs).then(function (r) { if (me === fseq) { found = r; draw(); } });
      }
      function draw() {
        clear(list);
        var sc = getScope(which);
        var shown = (found || []).slice();
        // 已经选了的排在最前面
        var picked = docs.filter(function (d) { return sc.docs.indexOf(d.id) >= 0; });
        shown = picked.concat(shown.filter(function (d) { return sc.docs.indexOf(d.id) < 0; }));
        if (!shown.length) { list.appendChild(h('p', { class: 'muted small dp-empty', text: docs.length ? '没找到。换个词试试。' : '资料库里还没有文件。' })); return; }
        shown.forEach(function (d) {
          var on = sc.docs.indexOf(d.id) >= 0;
          list.appendChild(h('button', { class: 'menu-item scope-item dp-item' + (on ? ' is-checked' : ''), type: 'button', role: 'menuitemcheckbox',
            'aria-checked': on ? 'true' : 'false', on: { click: function (e) {
              var idx = Array.prototype.indexOf.call(list.querySelectorAll('.menu-item'), e.currentTarget);
              var cur = getScope(which);
              cur.docs = toggleIn(cur.docs, d.id);
              S.docNames[d.id] = d.title || fileName(d.path);
              return Promise.resolve(setScope(which, cur, true)).then(function () {
                draw();
                var again = list.querySelectorAll('.menu-item')[idx];
                if (again) again.focus();
              });
            } } },
            h('span', { class: 'scope-check', 'aria-hidden': 'true' }, on ? icon('check', 12) : null),
            h('span', { class: 'mi-label', text: d.title || fileName(d.path) }),
            h('span', { class: 'mi-desc', text: [d.library, summaryLine(d, 60)].filter(Boolean).join(' · ') })));
        });
      }
      q.addEventListener('input', function () { search(); });
      q.addEventListener('keydown', function (e) {
        if (e.isComposing || e.keyCode === 229) return;
        if (e.key === 'Enter') { e.preventDefault(); var f = list.querySelector('.menu-item'); if (f) f.click(); }
      });
      m.appendChild(h('div', { class: 'menu-caption', text: '只在这几份文件里找' }));
      m.appendChild(q);
      m.appendChild(list);
      m.appendChild(h('div', { class: 'dp-foot' },
        h('span', { class: 'small muted', text: '上下键选，回车勾上' }),
        h('button', { class: 'menu-item dp-done', type: 'button', text: '好了', on: { click: function () { closeMenus(true); } } })));
      loadAllDocs().then(function (all) { docs = all; return search(); });
      setTimeout(function () { q.focus(); }, 0);
    });
  }

  /* ---------- 输入 # 选文件（和 / 弹指令一样的做法） ---------- */

  var HP = { which: null, tok: null, rows: [], sel: 0, off: null, docs: null, at: 0, my: 0 };

  function hashUpdate(which) {
    var input = composerFor(which).input;
    if (!input) return;
    var usable = S.mode === 'hspace' && !S.libsMissing && scopeV2() && scopeLibs().length &&
      (which === 'home' || (S.view && S.sid && (S.view.mode === 'hspace' || !S.view.mode)));
    var tok = usable && !parseSlash(input.value) && document.activeElement === input ? hashQuery(input.value, input.selectionStart) : null;
    if (!tok) { HP.off = null; if (HP.which === which) closeHash(); return; }
    if (HP.off === which + ':' + tok.start) return;       // 按过 Esc：这个 # 当普通文字
    HP.which = which;
    HP.tok = tok;
    var my = ++HP.my;
    var draw = function () {
      if (my !== HP.my || HP.which !== which) return;
      findDocs(tok.q, 8, HP.docs || []).then(function (rows) {
        if (my !== HP.my || HP.which !== which) return;
        HP.rows = rows;
        if (HP.sel >= HP.rows.length) HP.sel = 0;
        drawHash();
      });
    };
    if (HP.docs && Date.now() - HP.at < 60000) draw();
    else {
      HP.rows = []; drawHash(true);
      loadAllDocs().then(function (all) { HP.docs = all; HP.at = Date.now(); draw(); });
    }
  }

  function hashRowId(i) { return 'hash-opt-' + i; }

  function drawHash(loading) {
    var menu = $('hash-menu'), which = HP.which, input = which ? composerFor(which).input : null;
    if (!menu || !input) return;
    clear(menu);
    menu.appendChild(h('div', { class: 'slash-group', role: 'presentation', text: '选一份文件：只在它里面找' }));
    if (loading) menu.appendChild(h('div', { class: 'slash-item slash-info', role: 'presentation' }, h('span', { class: 'slash-desc', text: '正在读取文件列表…' })));
    else if (!HP.rows.length) menu.appendChild(h('div', { class: 'slash-item slash-info', role: 'presentation' },
      h('span', { class: 'slash-desc', text: HP.tok && HP.tok.q ? '没有叫「' + HP.tok.q + '」的文件。Esc 关掉，# 当普通文字' : '资料库里还没有文件' })));
    var picked = getScope(which).docs;
    HP.rows.forEach(function (d, i) {
      var sel = i === HP.sel;
      menu.appendChild(h('div', { class: 'slash-item slash-opt' + (sel ? ' is-sel' : '') + (picked.indexOf(d.id) >= 0 ? ' is-current' : ''),
        id: hashRowId(i), role: 'option', 'aria-selected': sel ? 'true' : 'false',
        on: { mousedown: function (e) { e.preventDefault(); }, click: function () { HP.sel = i; return hashConfirm(which); },
              mousemove: function () { if (HP.sel !== i) { HP.sel = i; drawHash(); } } } },
        h('span', { class: 'slash-name' }, h('span', { text: d.title || fileName(d.path) }), picked.indexOf(d.id) >= 0 ? h('span', { class: 'slash-alias', text: '已选' }) : null),
        h('span', { class: 'slash-desc', text: [d.library, summaryLine(d, 50)].filter(Boolean).join(' · ') })));
    });
    menu.appendChild(h('div', { class: 'slash-foot', role: 'presentation', text: '上下键选择，回车或 Tab 确认，Esc 关闭' }));
    menu.hidden = false;
    input.setAttribute('aria-expanded', 'true');
    input.setAttribute('aria-controls', 'hash-menu');
    if (HP.rows.length) input.setAttribute('aria-activedescendant', hashRowId(HP.sel)); else input.removeAttribute('aria-activedescendant');
    placeSlash(menu, which);
  }

  function closeHash() {
    var menu = $('hash-menu'), which = HP.which;
    HP.which = null; HP.tok = null; HP.rows = []; HP.sel = 0;
    if (menu && !menu.hidden) { menu.hidden = true; clear(menu); }
    var input = which ? composerFor(which).input : null;
    if (input) { input.setAttribute('aria-expanded', 'false'); input.setAttribute('aria-controls', 'slash-menu'); input.removeAttribute('aria-activedescendant'); }
  }

  /** 输入框上的按键（# 菜单开着时）。返回 true 表示处理了。输入法组字时一律不管。 */
  function hashKey(which, e) {
    if (HP.which !== which || e.isComposing || e.keyCode === 229) return false;
    var k = e.key;
    if (k === 'Escape') { e.preventDefault(); e.stopPropagation(); HP.off = which + ':' + (HP.tok ? HP.tok.start : -1); closeHash(); return true; }
    if ((k === 'ArrowDown' || k === 'ArrowUp') && HP.rows.length) {
      e.preventDefault();
      HP.sel = (HP.sel + (k === 'ArrowDown' ? 1 : HP.rows.length - 1)) % HP.rows.length;
      drawHash();
      return true;
    }
    if (((k === 'Enter' && !e.shiftKey) || (k === 'Tab' && !e.shiftKey)) && HP.rows.length) {
      e.preventDefault();
      var r = hashConfirm(which);
      if (r && typeof r.then === 'function') r.then(null, report);
      return true;
    }
    return false;
  }

  /** 选中一份文件：加进"指定文件"，把 #… 从输入框里去掉。 */
  function hashConfirm(which) {
    var d = HP.rows[HP.sel], input = composerFor(which).input;
    if (!d || !input) return;
    var res = dropHashToken(input.value, HP.tok);
    closeHash();
    input.value = res.value;
    try { input.focus(); input.setSelectionRange(res.caret, res.caret); } catch (e) { /* 忽略 */ }
    if (which !== 'home') autosize(input);
    updateSendBtn(which);
    S.docNames[d.id] = d.title || fileName(d.path);
    var cur = getScope(which);
    if (cur.docs.indexOf(d.id) >= 0) return;
    cur.docs = cur.docs.concat([d.id]);
    return setScope(which, cur, true);
  }

  /** 新建资料库：先弹系统的选择文件夹窗口，选好后用文件夹名建库并开始收录，去它的详情页。
   *  这台电脑弹不了窗口时去知识库页，用手填路径的表单。 */
  function newLibraryFlow(name) {
    if (S.picking) { toast('选择文件夹的窗口已经开着了。没看到的话，它可能在浏览器后面。', 'info'); return; }
    if (S.noPicker) { S.kbNewOpen = true; go('#/kb'); return; }
    S.picking = true;
    if (S.route === 'home') renderHomeKb();
    var note = pickNote('请在弹出的窗口里选择放资料的文件夹…（没看到的话，它可能在浏览器后面）');
    function done() { S.picking = false; note(); if (S.route === 'home') renderHomeKb(); }
    return api('POST', '/api/pick_folder', {}).then(function (d) {
      done();
      if (!d || d.supported === false) { S.noPicker = true; S.kbNewOpen = true; go('#/kb'); return; }
      if (!d.path) return;
      return createLibrary(str(name).trim() || str(d.name) || fileName(str(d.path).replace(/[\\\/]+$/, '')), str(d.path));
    }, function (e) {
      done();
      if (e && e.status === 409) { toast('选择文件夹的窗口已经开着了。', 'info'); return; }
      if (e && (e.missing || e.status === 404)) { S.noPicker = true; S.kbNewOpen = true; go('#/kb'); return; }
      throw e;
    });
  }

  function createLibrary(name, path) {
    return api('POST', '/api/libraries', { name: str(name).slice(0, 60) || '资料库' }).then(function (lib) {
      lib = lib && lib.library ? lib.library : lib;
      if (!lib || !lib.id) throw new Error('资料库没有建起来，请再试一次。');
      var next = path ? api('POST', '/api/libraries/' + encodeURIComponent(lib.id) + '/sources', { path: path }) : Promise.resolve();
      return next.then(function () {
        return loadLibrariesFresh().then(function () {
          toast(path ? '已建好「' + (lib.name || name) + '」，正在收录。' : '已建好「' + (lib.name || name) + '」。', 'info');
          go('#/kb/' + encodeURIComponent(lib.id));
        });
      });
    });
  }

  function loadLibrariesFresh() { libsReq = null; return loadLibraries(); }

  /* ---------------- 深入研究开关 ---------------- */

  function renderDeep() {
    ['home', 'composer'].forEach(function (which) {
      var b = $(which + '-deep');
      if (!b) return;
      var on = S.mode === 'hspace' && (which === 'home' || !S.view || S.view.mode === 'hspace' || !S.view.mode);
      b.hidden = !on;
      if (!on) { S.deep[which] = false; return; }
      b.setAttribute('aria-pressed', S.deep[which] ? 'true' : 'false');
      b.classList.toggle('is-on', !!S.deep[which]);
      b.title = S.deep[which] ? '深入研究：开着。这一条会拆成小问题、多种问法查，写成有结构的回答。再点关掉'
        : '深入研究：拆成小问题、多种问法查，写成有结构的回答。只对下一条消息有效';
    });
  }

  /* ---------------- 出处标签和提示 ---------------- */

  /** 在渲染好的回答里，把确实存在的 [n] 换成出处标签。代码、链接里的不动。返回标出来的编号。 */
  function decorateCites(md, v, g) {
    var hits = (v && v.hits) || {};
    if (!Object.keys(hits).length) return;
    function has(n) { return !!hits[n]; }
    var walker = document.createTreeWalker(md, 4, null);   // 4 = NodeFilter.SHOW_TEXT
    var nodes = [], node;
    while ((node = walker.nextNode())) {
      if (!/\[\d/.test(node.nodeValue)) continue;
      var p = node.parentNode;
      if (p && p.closest && p.closest('code, pre, a')) continue;
      nodes.push(node);
    }
    nodes.forEach(function (tn) {
      var parts = splitCites(tn.nodeValue, has);
      if (parts.length === 1 && parts[0].text != null) return;
      var frag = document.createDocumentFragment();
      parts.forEach(function (pt) {
        if (pt.text != null) { frag.appendChild(document.createTextNode(pt.text)); return; }
        var grp = h('span', { class: 'cites' });
        pt.cite.forEach(function (n) { grp.appendChild(citeChip(n, hits[n], citeWarn(g, n))); });
        frag.appendChild(grp);
      });
      tn.parentNode.replaceChild(frag, tn);
    });
  }

  function citeChip(n, hit, warn) {
    var b = h('button', { class: 'cite' + (warn ? ' has-warn' : '') + (hit.superseded ? ' is-old' : '') + (hit.standard ? ' is-std' : ''),
      type: 'button', 'data-n': String(n), 'data-fk': 'cite' + n,
      'aria-label': '出处 ' + n + '：' + hitWhere(hit) + (warn ? '。' + warn : '') + '。点开看原文',
      on: { click: function (e) { e.preventDefault(); hideTip(); openSources(n); } } },
      String(n), warn ? h('span', { class: 'cite-dot', 'aria-hidden': 'true' }) : null);
    b.addEventListener('mouseenter', function () { showTip(b, hit, warn); });
    b.addEventListener('focus', function () { showTip(b, hit, warn); });
    b.addEventListener('mouseleave', hideTip);
    b.addEventListener('blur', hideTip);
    return b;
  }

  function showTip(anchor, hit, warn) {
    var tip = $('cite-tip');
    if (!tip || !hit) return;
    clear(tip);
    tip.appendChild(h('span', { class: 'ct-title', text: hit.title }));
    var loc = [hit.loc || hit.heading, hit.library].filter(Boolean).join(' · ');
    if (loc) tip.appendChild(h('span', { class: 'ct-loc', text: loc }));
    var badges = hitBadges(hit);
    if (badges.length || hit.superseded) {
      tip.appendChild(h('span', { class: 'ct-badges' }, badges.map(function (b) { return h('span', { class: 'kb-badge tone-' + (b.tone || 'plain'), text: b.text }); }),
        hit.superseded ? h('span', { class: 'ct-newer', text: newerText(hit) }) : null));
    }
    if (warn) tip.appendChild(h('span', { class: 'ct-warn', text: warn }));
    tip.hidden = false;
    var r = anchor.getBoundingClientRect(), th = tip.offsetHeight, tw = tip.offsetWidth;
    var vw = window.innerWidth || 1200;
    var top = r.top - th - 8;
    if (top < 8) top = r.bottom + 8;
    tip.style.top = top + 'px';
    tip.style.left = Math.max(8, Math.min(r.left + r.width / 2 - tw / 2, vw - tw - 8)) + 'px';
    anchor.setAttribute('aria-describedby', 'cite-tip');
  }

  function hideTip() {
    var tip = $('cite-tip');
    if (tip) tip.hidden = true;
  }

  /** 回答下面那一行："依据 3 处资料 · 有 1 个数字没对上"。依据那段可以点，打开来源面板。 */
  function basisLine(parts, nums) {
    var line = h('div', { class: 'basis' });
    parts.forEach(function (p, i) {
      if (i) line.appendChild(h('span', { class: 'basis-sep', 'aria-hidden': 'true', text: '·' }));
      if (i === 0 && nums.length) {
        line.appendChild(h('button', { class: 'basis-src', type: 'button', 'data-fk': 'basis', title: '在右边看这几处原文',
          on: { click: function () { openSources(nums[0]); } } }, icon('book', 13), h('span', { text: p.text })));
      } else {
        line.appendChild(h('span', { class: 'basis-part' + (p.tone ? ' is-' + p.tone : '') },
          p.tone === 'warn' ? h('span', { class: 'basis-dot', 'aria-hidden': 'true' }) : null, p.text));
      }
    });
    return line;
  }

  /* ---------------- 反馈（赞、踩、记成标准答案） ---------------- */

  var FB = { missing: false, ui: {} };    // ui：回答序号 → {open, busy, text}

  function questionFor(v, it) {
    var idx = v.items.indexOf(it), q = '';
    for (var i = idx - 1; i >= 0; i--) if (v.items[i].kind === 'user') { q = v.items[i].text; break; }
    return q;
  }

  function rate(v, it, rating) {
    if (FB.missing || !it.seq) return;
    var sid = v.sid, seq = it.seq;
    var old = v.feedback[seq] || null;
    var cur = old ? old.rating : null;
    var next = cur === rating ? null : rating;
    v.feedback[seq] = { rating: next, note: old ? old.note : '' };
    var ui = FB.ui[seq] || (FB.ui[seq] = { open: false, busy: false, text: '' });
    ui.open = next === 'down';
    rerender();
    if (next === 'down') setTimeout(function () { var t = document.querySelector('[data-fb-note="' + seq + '"]'); if (t) t.focus(); }, 0);
    return api('POST', '/api/sessions/' + encodeURIComponent(sid) + '/feedback', { seq: seq, rating: next }).then(null, function (e) {
      if (S.view && S.view.sid === sid) { v.feedback[seq] = old; ui.open = false; rerender(); }
      if (e && (e.missing || e.status === 404 || e.status === 405)) { FB.missing = true; rerender(); return; }   // 老后端：不显示
      throw e;
    });
  }

  function sendNote(v, it, asStandard) {
    var ui = FB.ui[it.seq];
    if (!ui) return;
    var text = str(ui.text).trim();
    if (!text) { toast('先写一下正确的答案是什么。', 'info'); return; }
    ui.busy = true;
    rerender();
    var sid = v.sid, seq = it.seq;
    var p = api('POST', '/api/sessions/' + encodeURIComponent(sid) + '/feedback', { seq: seq, rating: 'down', note: text });
    if (asStandard) {
      p = p.then(function () {
        return api('POST', '/api/kb/answers', { question: questionFor(v, it), answer: text, session_id: sid, seq: seq });
      });
    }
    return p.then(function () {
      ui.busy = false; ui.open = false;
      v.feedback[seq] = { rating: 'down', note: text };
      rerender();
      toast(asStandard ? '记成标准答案了。下次同样的问题按它回答。' : '反馈记下了。', 'info');
    }, function (e) {
      ui.busy = false;
      rerender();
      if (e && (e.missing || e.status === 404)) throw new Error('程序还不支持记标准答案，先只记下了反馈。');
      throw e;
    });
  }

  function feedbackButtons(v, it) {
    if (FB.missing || !it.seq) return null;
    var fb = v.feedback[it.seq] || {}, r = fb.rating;
    return [
      h('button', { class: 'msg-act fb-btn' + (r === 'up' ? ' is-on' : ''), type: 'button', 'data-fk': 'up',
        'aria-pressed': r === 'up' ? 'true' : 'false', title: r === 'up' ? '取消"有用"' : '这条回答有用', 'aria-label': '有用',
        on: { click: function () { return rate(v, it, 'up'); } } }, icon('thumbUp', 14)),
      h('button', { class: 'msg-act fb-btn' + (r === 'down' ? ' is-on' : ''), type: 'button', 'data-fk': 'down',
        'aria-pressed': r === 'down' ? 'true' : 'false', title: r === 'down' ? '取消"不对"' : '这条回答不对', 'aria-label': '不对',
        on: { click: function () { return rate(v, it, 'down'); } } }, icon('thumbDown', 14))
    ];
  }

  function feedbackBox(v, it) {
    var ui = FB.ui[it.seq];
    if (FB.missing || !ui || !ui.open) return null;
    var ta = h('textarea', { class: 'fb-note', rows: 3, placeholder: '正确的答案是……', 'aria-label': '正确的答案是', 'data-fb-note': String(it.seq),
      value: ui.text, disabled: ui.busy });
    ta.addEventListener('input', function () { ui.text = ta.value; });
    return h('div', { class: 'fb-box' }, h('p', { class: 'fb-hint', text: '哪里不对？写下正确的答案。记成标准答案后，下次同样的问题按它回答。' }), ta,
      h('div', { class: 'fb-actions' },
        h('button', { class: 'btn btn-primary btn-sm', type: 'button', 'data-fk': 'std', disabled: ui.busy, text: '记成标准答案',
          title: '下次有人问同样的问题，按这个回答', on: { click: function () { return sendNote(v, it, true); } } }),
        h('button', { class: 'btn btn-sm', type: 'button', 'data-fk': 'note', disabled: ui.busy, text: '只记下反馈',
          on: { click: function () { return sendNote(v, it, false); } } }),
        h('button', { class: 'btn btn-ghost btn-sm', type: 'button', 'data-fk': 'cancel', disabled: ui.busy, text: '算了',
          on: { click: function () { ui.open = false; rerender(); } } })));
  }

  /* ---------------- 来源面板 ---------------- */

  /** 来源面板里要列的：引用过的；一个都没引用时列这个对话检索到的全部。 */
  function sourceList(v) {
    var cited = citedSources(v);
    if (cited.length) return { list: cited, cited: true };
    var hits = v.hits || {};
    return { list: Object.keys(hits).map(Number).sort(function (a, b) { return a - b; }).map(function (n) { return hits[n]; }), cited: false };
  }

  function allWarn(v, n) {
    var out = [];
    Object.keys(v.grounding || {}).forEach(function (k) {
      var m = v.grounding[k].missing[n];
      (m || []).forEach(function (x) { if (out.indexOf(x) < 0) out.push(x); });
    });
    return out;
  }

  function openSources(n) {
    S.srcFocus = n || 0;
    S.preview = null;
    if (!isWide()) { document.body.classList.add('show-right'); syncScrim(); }
    else if (!S.rightOpen) { S.rightOpen = true; applyRight(); }
    S.rightSig = null;
    if (S.view) renderTask();
    var el = n ? $('src-' + n) : null;
    if (el) {
      try { el.scrollIntoView({ block: 'nearest', behavior: reducedMotion() ? 'auto' : 'smooth' }); } catch (e) { el.scrollIntoView(); }
      try { el.focus({ preventScroll: true }); } catch (e2) { el.focus(); }
    }
  }

  function kbFileUrl(docId) {
    return apiUrl('/api/kb/docs/' + encodeURIComponent(docId) + '/file', { t: S.token });
  }

  function badgeEls(hit) {
    return hitBadges(hit).map(function (b) { return h('span', { class: 'kb-badge tone-' + (b.tone || 'plain'), text: b.text }); });
  }

  function renderSources(box, v) {
    var sl = sourceList(v);
    clear(box);
    if (!sl.cited) box.appendChild(h('p', { class: 'rp-empty src-note', text: '回答里还没有引用。下面是这个对话检索到的片段。' }));
    var ol = h('ol', { class: 'src-list' });
    sl.list.forEach(function (hit) {
      var miss = allWarn(v, hit.n);
      var focus = hit.n === S.srcFocus;
      ol.appendChild(h('li', { class: 'src' + (focus ? ' is-focus' : '') + (hit.superseded ? ' is-old' : ''), id: 'src-' + hit.n, tabindex: '-1' },
        h('div', { class: 'src-head' },
          h('span', { class: 'src-n', text: String(hit.n) }),
          h('span', { class: 'src-title', text: hit.title })),
        h('div', { class: 'src-loc' }, [hit.loc || hit.heading, hit.library].filter(Boolean).join(' · '), badgeEls(hit)),
        hit.superseded ? h('div', { class: 'src-newer', text: newerText(hit) }) : null,
        hit.text ? h('blockquote', { class: 'src-text', text: clip(hit.text.replace(/\s+\n/g, '\n'), 360) }) : null,
        miss.length ? h('div', { class: 'src-warn' }, h('span', { class: 'basis-dot', 'aria-hidden': 'true' }), '这个数字在出处里没找到：' + miss.join('、')) : null,
        h('div', { class: 'src-foot' },
          hit.via ? h('span', { class: 'src-via', title: hit.score != null ? '分数 ' + hit.score.toFixed(3) : null, text: '靠' + viaLabel(hit.via) + '找到' }) : null,
          h('span', { class: 'src-acts' },
            hit.part_id ? h('button', { class: 'src-act', type: 'button', text: '看整节', on: { click: function () { return openPart(hit); } } }) : null,
            hit.doc_id ? h('a', { class: 'src-act', href: kbFileUrl(hit.doc_id), target: '_blank', rel: 'noopener', text: '打开文件' }) : null))));
    });
    box.appendChild(ol);
  }

  /** 看整节：在右栏里显示这个片段所在的整节，片段本身标出来。 */
  function openPart(hit) {
    S.preview = { kind: 'part', hit: hit, loading: true, data: null, error: '' };
    if (!isWide()) { document.body.classList.add('show-right'); syncScrim(); }
    else if (!S.rightOpen) { S.rightOpen = true; applyRight(); }
    renderPreview();
    if (S.view) renderTask();
    var want = S.preview;
    return api('GET', '/api/kb/parts/' + encodeURIComponent(hit.part_id)).then(function (d) {
      if (S.preview !== want) return;
      want.loading = false; want.data = d || {};
      renderPreview();
    }, function (e) {
      if (S.preview !== want) return;
      want.loading = false; want.error = (e && e.message) || '没有读到这一节。';
      renderPreview();
    });
  }

  /** GET /api/kb/parts/<id> 的返回 → {heading, loc, text}。形状认几种：{part, section} / {section_text} / {text}。 */
  function partSection(d, hit) {
    d = d && typeof d === 'object' ? d : {};
    var sec = d.section && typeof d.section === 'object' ? d.section : d.parent && typeof d.parent === 'object' ? d.parent : {};
    var part = d.part && typeof d.part === 'object' ? d.part : d;
    return { heading: str(sec.heading || part.heading || hit.heading), loc: str(sec.loc || part.loc || hit.loc),
             text: str(sec.text || d.section_text || part.text || hit.text), title: str(part.title || d.title || hit.title) };
  }

  function renderPartPreview(pv, p) {
    var hit = p.hit;
    pv.appendChild(h('div', { class: 'pv-head' },
      h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '返回来源', on: { click: closePreview } }),
      hit.doc_id ? h('a', { class: 'btn btn-sm', href: kbFileUrl(hit.doc_id), target: '_blank', rel: 'noopener', text: '打开文件' }) : null));
    var sec = partSection(p.data, hit);
    pv.appendChild(h('div', { class: 'pv-title' }, h('span', { class: 'src-n', text: String(hit.n || '') }), h('span', { class: 'pv-name', text: sec.title })));
    pv.appendChild(h('div', { class: 'pv-path' }, [sec.loc, sec.heading !== sec.loc ? sec.heading : ''].filter(Boolean).join(' · '), badgeEls(hit)));
    if (hit.superseded) pv.appendChild(h('div', { class: 'src-newer', text: newerText(hit) }));
    if (p.loading) { pv.appendChild(h('p', { class: 'muted', text: '正在读取…' })); return; }
    if (p.error) { pv.appendChild(h('p', { class: 'form-error', text: p.error })); return; }
    var text = sec.text, frag = str(hit.text).trim(), body = h('div', { class: 'pv-text pv-section' });
    var at = frag ? text.indexOf(frag) : -1;
    if (at >= 0) {
      body.appendChild(document.createTextNode(text.slice(0, at)));
      body.appendChild(h('mark', { class: 'pv-hit', text: frag }));
      body.appendChild(document.createTextNode(text.slice(at + frag.length)));
    } else body.textContent = text || '这一节没有文字。';
    pv.appendChild(body);
    if (at >= 0) setTimeout(function () { var m = pv.querySelector('.pv-hit'); if (m && m.scrollIntoView) m.scrollIntoView({ block: 'center' }); }, 0);
  }

  /** 检索步骤展开后：找到的片段，每条一行（编号、标题、出处），点了在来源面板里看。 */
  function kbHitList(nums) {
    var hits = (S.view && S.view.hits) || {};
    var ol = h('ol', { class: 'kb-hitlist' });
    nums.forEach(function (n) {
      var hit = hits[n];
      if (!hit) return;
      ol.appendChild(h('li', {},
        h('button', { class: 'kb-hit', type: 'button', title: '在右边看原文', on: { click: function () { openSources(n); } } },
          h('span', { class: 'src-n', text: String(n) }),
          h('span', { class: 'kb-hit-title', text: hit.title }),
          h('span', { class: 'kb-hit-loc', text: hit.loc || hit.heading }), badgeEls(hit))));
    });
    return ol;
  }

  /* ---------------- 知识库页（有两种模式的新后端才有；老后端是原来的"资料"页） ---------------- */

  function pageLibraries(body, alive, libId) {
    if (!S.modes.length) return pageKb(body, alive);
    S.kbLive = null;
    if (libId) return pageLibrary(body, alive, libId);
    var answersP = api('GET', '/api/kb/answers').then(function (d) { return { list: asList(d, 'answers') }; }, function (e) {
      return { missing: !!(e && (e.missing || e.status === 404 || e.status === 405)), error: e };
    });
    // 第 4 节的两栏：老后端没有接口（404 / 405）就不显示
    var gapsP = api('GET', apiUrl('/api/kb/gaps', { status: 'open' })).then(function (d) { return { list: normGaps(d) }; }, missingOr);
    var evalP = api('GET', '/api/kb/eval').then(function (d) { return { data: normEval(d) }; }, missingOr);
    return Promise.all([loadLibrariesFresh(), answersP, gapsP, evalP, loadShared()]).then(function (res) {
      if (!alive()) return;
      clear(body);
      if (S.libsMissing) { body.appendChild(emptyRow('程序还没有资料库功能。更新后再来。')); return; }
      body.appendChild(kbSearchSection());
      var libSec = libListSection(body, alive);
      body.appendChild(libSec);
      if (!res[2].missing) body.appendChild(gapsSection(res[2].list || [], res[2].error));
      if (!res[1].missing) body.appendChild(answersSection(res[1].list || [], res[1].error));
      if (!res[3].missing) body.appendChild(evalSection(res[3].data || { items: [], runs: [] }, res[3].error));
      // 有资料库在收录：收进度事件；没有事件时每 3 秒查一次
      S.kbLive = function () { if (alive()) drawLibRows(); };
      pollWhile(alive, function () { return (S.libs || []).some(function (l) { return l.state === 'indexing'; }); }, function () {
        return loadLibrariesFresh().then(function () { if (alive()) drawLibRows(); });
      });
      function drawLibRows() { var box = libSec.querySelector('.lib-list'); if (box) fillLibList(box); }
    });
  }

  /** 请求失败：老后端没有这个接口 → {missing: true}（对应的部分不显示）；别的错 → {error}。 */
  function missingOr(e) {
    return { missing: !!(e && (e.missing || e.status === 404 || e.status === 405)), error: e };
  }

  /** cond() 为真时每 3 秒做一次 fn（最近 4 秒内收到过进度事件就跳过这一次），页面换掉就停。 */
  function pollWhile(alive, cond, fn) {
    function tick() {
      if (!alive()) return;
      if (!cond()) { setTimeout(tick, 6000); return; }
      var p = Date.now() - S.lastKbEvent < 4000 ? Promise.resolve() : Promise.resolve(fn()).then(null, function () {});
      p.then(function () { setTimeout(tick, 3000); });
    }
    setTimeout(tick, 3000);
  }

  function libStatePill(lib) {
    var st = libraryStateText(lib);
    return h('span', { class: 'lib-state tone-' + st.tone }, h('span', { class: 'lib-dot', 'aria-hidden': 'true' }), st.text);
  }

  function progressBar(lib) {
    var p = lib.progress || {};
    if (lib.state !== 'indexing') return null;
    var pct = p.total ? Math.round(Math.min(1, (p.done + p.failed) / p.total) * 100) : 0;
    return h('div', { class: 'lib-progress' + (p.total ? '' : ' is-unknown'), role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100',
      'aria-valuenow': p.total ? String(pct) : null, 'aria-label': '收录进度' },
      h('span', { class: 'lib-progress-fill', style: 'width:' + (p.total ? pct : 30) + '%' }));
  }

  function syncedText(lib) {
    var t = lib.synced || lib.updated;
    return typeof t === 'number' && t > 0 ? (lib.synced ? '上次同步 ' : '更新于 ') + relativeTime(t) : '';
  }

  /* ---------- 公司共享目录（第 5 节）：知识库页和设置页都用 ---------- */

  /** GET /api/shared → S.shared（normShared）；老后端没有这个接口时 S.shared = {missing: true}。不报错。 */
  function loadShared(force) {
    if (S.shared && !force && Date.now() - (S.sharedAt || 0) < 30000) return Promise.resolve(S.shared);
    return api(force === 'check' ? 'POST' : 'GET', force === 'check' ? '/api/shared/check' : '/api/shared').then(function (d) {
      S.shared = normShared(d); S.sharedAt = Date.now(); return S.shared;
    }, function (e) {
      if (e && (e.missing || e.status === 404 || e.status === 405)) { S.shared = { missing: true }; S.sharedAt = Date.now(); return S.shared; }
      if (force === 'check') throw e;
      return S.shared || { missing: true };
    });
  }

  function sharedLibOf(lib) {
    if (!lib || !lib.shared || !S.shared || !S.shared.libraries) return null;
    return S.shared.libraries.filter(function (x) { return x.id === lib.id; })[0] || null;
  }

  function libListSection(body, alive) {
    var sec = section('资料库', '每个资料库是一个或几个文件夹。Hspace 回答问题时从这里查，并标出处。');
    var head = sec.querySelector('.panel-title');
    var newBtn = h('button', { class: 'btn btn-sm', type: 'button', text: '新建资料库', 'aria-expanded': 'false' });
    var wrap = h('div', { class: 'panel-head' });
    sec.insertBefore(wrap, head);
    wrap.appendChild(head);
    wrap.appendChild(newBtn);
    var form = newLibraryForm(function () { newBtn.setAttribute('aria-expanded', 'false'); form.hidden = true; });
    form.hidden = !S.kbNewOpen;
    newBtn.setAttribute('aria-expanded', S.kbNewOpen ? 'true' : 'false');
    S.kbNewOpen = false;
    newBtn.addEventListener('click', function () {
      form.hidden = !form.hidden;
      newBtn.setAttribute('aria-expanded', form.hidden ? 'false' : 'true');
      if (!form.hidden) { var i = form.querySelector('input'); if (i) i.focus(); }
    });
    sec.appendChild(form);
    var list = h('div', { class: 'lib-list' });
    fillLibList(list);
    sec.appendChild(list);
    return sec;
  }

  function fillLibList(box) {
    clear(box);
    var libs = S.libs || [];
    if (!libs.length) {
      box.appendChild(h('div', { class: 'list-empty lib-empty' },
        h('p', { text: '还没有资料库。把放 SOP、制度、历年汇报的文件夹加进来，Hspace 就能照着回答。' }),
        h('button', { class: 'btn btn-primary', type: 'button', text: '添加一个放 SOP 的文件夹', on: { click: function () { return newLibraryFlow(''); } } })));
      return;
    }
    libs.forEach(function (lib) {
      var src = lib.sources.length ? lib.sources[0].path + (lib.sources.length > 1 ? ' 等 ' + lib.sources.length + ' 处' : '') : '还没有来源文件夹';
      var sl = sharedLibOf(lib);
      if (lib.shared) src = (S.shared && S.shared.name ? S.shared.name + ' 的' : '公司') + '共享资料库';
      box.appendChild(h('a', { class: 'lib-row' + (lib.enabled ? '' : ' is-off'), href: buildHash(S.mode, 'kb/' + encodeURIComponent(lib.id)) },
        h('span', { class: 'lib-main' },
          h('span', { class: 'lib-name' }, h('span', { text: lib.name }),
            lib.shared ? h('span', { class: 'kb-badge tone-co', title: '公司统一建的资料库，大家共用', text: '公司' }) : null,
            lib.shared && (sl || lib.version) ? h('span', { class: 'lib-ver', text: sl ? sharedLibText(sl) : '第 ' + lib.version + ' 版' }) : null),
          h('span', { class: 'lib-src' + (lib.shared ? '' : ' mono'), text: src })),
        h('span', { class: 'lib-meta' },
          h('span', { class: 'lib-count', text: groupNum(lib.docs) + ' 份 · ' + groupNum(lib.parts) + ' 段' }),
          syncedText(lib) ? h('span', { class: 'lib-synced', text: syncedText(lib) }) : null),
        !lib.enabled ? h('span', { class: 'lib-state tone-empty' }, h('span', { class: 'lib-dot', 'aria-hidden': 'true' }), '已停用')
          : sl && sl.state === 'updating' ? h('span', { class: 'lib-state tone-busy' }, h('span', { class: 'lib-dot', 'aria-hidden': 'true' }), '正在同步新版本')
          : libStatePill(lib),
        progressBar(lib) || (sl && sl.state === 'updating' ? h('div', { class: 'lib-progress is-unknown', role: 'progressbar', 'aria-label': '同步进度' },
          h('span', { class: 'lib-progress-fill', style: 'width:30%' })) : null)));
    });
  }

  /** 新建资料库：名字 + 选文件夹（系统窗口）；弹不了窗口时多一栏手填路径。 */
  function newLibraryForm(onDone) {
    var name = h('input', { type: 'text', maxlength: 60, placeholder: '例如：公司 SOP' });
    var path = h('input', { type: 'text', class: 'mono', placeholder: '例如：/Volumes/共享盘/制度  或  \\\\fileserver\\制度' });
    var pathField = field('文件夹路径', path, '在访达或资源管理器里右键文件夹，复制它的路径，粘贴到这里。');
    pathField.hidden = !S.noPicker;
    var form = inlineForm([field('叫什么', name, '不填就用文件夹的名字'), pathField], S.noPicker ? '建好并开始收录' : '选文件夹…', function () {
      if (S.noPicker) {
        var p = path.value.trim();
        if (!p) throw new Error('请填写文件夹路径。');
        return createLibrary(name.value.trim() || fileName(p.replace(/[\\\/]+$/, '')), p).then(onDone);
      }
      return Promise.resolve(newLibraryFlow(name.value.trim())).then(function () {
        if (S.noPicker) { pathField.hidden = false; var b = form.querySelector('button[type="submit"]'); if (b) b.textContent = '建好并开始收录'; }
      });
    });
    form.classList.add('lib-new');
    return form;
  }

  /** 检索测试：输入一句话，看找到了什么、是关键词还是语义找到的、分数。 */
  function kbSearchSection() {
    var sec = section('检索测试', '问一句，看看能从资料里找到什么。回答不准时先在这里试试。');
    var q = h('input', { type: 'search', placeholder: '例如：总监出差住宿标准', 'aria-label': '要检索的一句话' });
    var out = h('div', { class: 'kbt-out', 'aria-live': 'polite' });
    var form = h('form', { class: 'kbt-form', role: 'search' }, q, h('button', { class: 'btn', type: 'submit', text: '检索' }));
    var my = 0;
    form.addEventListener('submit', guard(function (e) {
      e.preventDefault();
      var text = q.value.trim();
      if (!text) { q.focus(); return; }
      var me = ++my;
      clear(out);
      out.appendChild(h('p', { class: 'muted small', text: '正在检索…' }));
      return api('POST', '/api/kb/search', { query: text }).then(function (d) {
        if (me !== my) return;
        drawKbTest(out, d || {});
      }, function (ex) {
        if (me !== my) return;
        clear(out);
        out.appendChild(h('p', { class: 'form-error', text: (ex && ex.message) || '没有查成。' }));
      });
    }));
    sec.appendChild(form);
    sec.appendChild(out);
    return sec;
  }

  function drawKbTest(out, d) {
    clear(out);
    var hits = (Array.isArray(d.hits) ? d.hits : []).map(normKbHit).filter(Boolean);
    var used = d.used && typeof d.used === 'object' ? d.used : {};
    var how = [used.keyword !== false ? '关键词' : '', used.vector ? '语义' : '', used.rerank ? '重排' : ''].filter(Boolean).join(' + ');
    out.appendChild(h('p', { class: 'kbt-sum' },
      hits.length ? '找到 ' + hits.length + ' 条' : '什么都没找到', how ? h('span', { class: 'muted', text: ' · 用了' + how }) : null,
      typeof d.ms === 'number' ? h('span', { class: 'muted', text: ' · ' + Math.round(d.ms) + ' 毫秒' }) : null,
      !used.vector && gate('semantic_search').ok === false ? h('span', { class: 'kbt-hint', text: gate('semantic_search').fallback || '' }) : null));
    var ol = h('ol', { class: 'kbt-list' });
    hits.forEach(function (hit, i) {
      var full = h('div', { class: 'kbt-full', hidden: true });
      ol.appendChild(h('li', { class: 'kbt-hit' },
        h('div', { class: 'kbt-head' },
          h('span', { class: 'kbt-rank mono', text: String(i + 1) }),
          h('span', { class: 'src-title', text: hit.title }),
          h('span', { class: 'src-loc', text: hit.loc || hit.heading }),
          badgeEls(hit),
          h('span', { class: 'kbt-right' },
            hit.via ? h('span', { class: 'src-via is-' + hit.via, text: viaLabel(hit.via) }) : null,
            hit.score != null ? h('span', { class: 'kbt-score mono', title: '分数', text: hit.score.toFixed(3) }) : null)),
        hit.library ? h('div', { class: 'kbt-lib small muted', text: hit.library + (hit.superseded ? ' · ' + newerText(hit) : '') }) : null,
        h('blockquote', { class: 'src-text', text: clip(hit.text, 400) }),
        h('div', { class: 'src-foot' }, h('span', { class: 'src-acts' },
          hit.part_id ? h('button', { class: 'src-act', type: 'button', text: '看整节', 'aria-expanded': 'false', on: { click: function (e) {
            var b = e.currentTarget;
            if (!full.hidden) { full.hidden = true; b.setAttribute('aria-expanded', 'false'); b.textContent = '看整节'; return; }
            b.setAttribute('aria-expanded', 'true'); b.textContent = '收起整节';
            full.hidden = false; full.textContent = '正在读取…';
            return api('GET', '/api/kb/parts/' + encodeURIComponent(hit.part_id)).then(function (x) {
              full.textContent = partSection(x, hit).text || '这一节没有文字。';
            }, function (ex) { full.textContent = (ex && ex.message) || '没有读到。'; });
          } } }) : null,
          hit.doc_id ? h('a', { class: 'src-act', href: kbFileUrl(hit.doc_id), target: '_blank', rel: 'noopener', text: '打开文件' }) : null)),
        full));
    });
    out.appendChild(ol);
  }

  /** 标准答案：点踩时记下的正确答案，也能手动加、改、删。 */
  function answersSection(list, err) {
    var sec = section('标准答案', '点"不对"时记下的正确答案，也可以在这里手动加。检索时排在最前，Hspace 以它为准。');
    var box = h('div', { class: 'ans-list' });
    var rows = (Array.isArray(list) ? list : []).filter(function (a) { return a && a.id; });
    function reload() {
      return api('GET', '/api/kb/answers').then(function (d) { rows = asList(d, 'answers').filter(function (a) { return a && a.id; }); draw(); });
    }
    box._reload = reload;
    function draw() {
      clear(box);
      if (err && !rows.length) box.appendChild(h('p', { class: 'form-error', text: (err && err.message) || '没有读到标准答案。' }));
      else if (!rows.length) box.appendChild(emptyRow('还没有。回答不对时点"不对"，写下正确答案，再点"记成标准答案"。'));
      rows.forEach(function (a) { box.appendChild(answerRow(a, reload)); });
    }
    draw();
    sec.appendChild(box);
    var q = h('textarea', { rows: 2, placeholder: '例如：总监去上海出差，住宿一晚最多报多少？' });
    var ans = h('textarea', { rows: 3, placeholder: '例如：一线城市每晚不超过 800 元（《差旅管理办法（2026 版）》4.2）。' });
    var add = inlineForm([field('问题', q), field('答案', ans)], '加上', function () {
      if (!q.value.trim() || !ans.value.trim()) throw new Error('问题和答案都要写。');
      return api('POST', '/api/kb/answers', { question: q.value.trim(), answer: ans.value.trim() }).then(function () {
        q.value = ''; ans.value = '';
        add.hidden = true; addBtn.hidden = false;
        toast('加上了。', 'info');
        return reload();
      });
    });
    add.hidden = true;
    var addBtn = h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '手动加一条', on: { click: function () {
      add.hidden = false; addBtn.hidden = true; q.focus();
    } } });
    sec.appendChild(h('div', { class: 'form-actions' }, addBtn));
    sec.appendChild(add);
    return sec;
  }

  function answerRow(a, reload) {
    var row = h('div', { class: 'ans' });
    function view() {
      clear(row);
      append(row, [
        h('div', { class: 'ans-q', text: str(a.question) }),
        h('div', { class: 'ans-a', text: str(a.answer) }),
        h('div', { class: 'ans-foot' },
          h('span', { class: 'small muted ans-meta' },
            answerUseText(a) ? h('span', { class: 'ans-uses' + (a.hits > 0 ? ' is-used' : ''), title: a.last_hit ? '最近一次 ' + formatTime(a.last_hit) : null, text: answerUseText(a) }) : null,
            a.updated || a.created ? h('span', { text: '更新于 ' + shortDate(a.updated || a.created) }) : null),
          h('span', { class: 'ans-acts' },
            h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '改', on: { click: edit } }),
            h('button', { class: 'btn btn-ghost btn-sm btn-danger-text', type: 'button', text: '删',
              on: { click: confirmThen('删掉这条标准答案？', function () {
                return api('DELETE', '/api/kb/answers/' + encodeURIComponent(a.id)).then(reload);
              }) } })))]);
    }
    function edit() {
      clear(row);
      var q = h('textarea', { rows: 2, value: str(a.question) }), an = h('textarea', { rows: 3, value: str(a.answer) });
      row.appendChild(inlineForm([field('问题', q), field('答案', an)], '保存', function () {
        return api('PATCH', '/api/kb/answers/' + encodeURIComponent(a.id), { question: q.value.trim(), answer: an.value.trim() }).then(function () {
          a.question = q.value.trim(); a.answer = an.value.trim(); view();
        });
      }));
      row.appendChild(h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '算了', on: { click: view } }));
      q.focus();
    }
    view();
    return row;
  }

  /* ---------- 没答上来的问题（第 4.1 节） ---------- */

  function gapsSection(list, err) {
    var sec = section('', '大家问了、资料里却答不上来的问题。写个答案存成标准答案，下次就能答上；不相干的点"不用管"。');
    var title = h('h2', { class: 'panel-title' }, '没答上来的问题', h('span', { class: 'count-pill', 'aria-label': '还有几条' }));
    sec.replaceChild(title, sec.querySelector('.panel-title'));
    var rows = (list || []).slice();
    var acts = h('div', { class: 'panel-acts' });
    var head = h('div', { class: 'panel-head' });
    sec.insertBefore(head, title);
    head.appendChild(title);
    head.appendChild(acts);
    var box = h('div', { class: 'gap-list' });
    sec.appendChild(box);
    function reload() {
      return api('GET', apiUrl('/api/kb/gaps', { status: 'open' })).then(function (d) { rows = normGaps(d); draw(); });
    }
    function drawActs() {
      clear(acts);
      acts.appendChild(h('a', { class: 'btn btn-ghost btn-sm', href: apiUrl('/api/kb/gaps/export', { t: S.token }), download: '没答上来的问题.csv',
        title: '下载成表格（Excel 能直接打开）', text: '导出' }));
      if (S.shared && S.shared.configured && !S.shared.missing) {
        acts.appendChild(h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '交给管资料的人',
          title: '把还开着的这些问题写到公司共享目录里，管资料的人能看到', on: { click: function (e) {
            var b = e.currentTarget; b.disabled = true;
            return api('POST', '/api/kb/gaps/submit', {}).then(function (r) {
              b.disabled = false;
              toast('交上去了' + (r && r.count != null ? '：' + r.count + ' 个问题' : '') + '。管资料的人会在共享目录里看到' + (r && r.path ? '（' + r.path + '）' : '') + '。', 'info');
            }, function (ex) { b.disabled = false; throw ex; });
          } } }));
      }
    }
    function draw() {
      var pill = title.querySelector('.count-pill');
      pill.textContent = rows.length ? String(rows.length) : '';
      pill.hidden = !rows.length;
      drawActs();
      clear(box);
      if (err && !rows.length) { box.appendChild(h('p', { class: 'form-error', text: (err && err.message) || '没有读到。' })); return; }
      if (!rows.length) { box.appendChild(emptyRow('现在没有。Hspace 答不上来、或者回答被点了"不对"的问题会记在这里。')); return; }
      rows.forEach(function (g) { box.appendChild(gapRow(g, function () { rows = rows.filter(function (x) { return x.id !== g.id; }); draw(); })); });
    }
    draw();
    sec._reload = reload;
    return sec;
  }

  function gapRow(g, gone) {
    var row = h('div', { class: 'gap' });
    function view() {
      clear(row);
      append(row, [
        h('div', { class: 'gap-q', text: g.question }),
        h('div', { class: 'gap-meta' },
          h('span', { class: 'kb-badge' + (g.reason === 'down' ? ' tone-warn' : ''), text: gapReasonText(g.reason) || '没答上来' }),
          h('span', { class: 'num', text: '问了 ' + groupNum(g.count) + ' 次' }),
          g.last_ts ? h('span', { title: g.first_ts ? '第一次 ' + formatTime(g.first_ts) : null, text: '最近 ' + relativeTime(g.last_ts) }) : null,
          g.note ? h('span', { class: 'gap-note', text: '备注：' + g.note }) : null),
        h('div', { class: 'gap-acts' },
          h('button', { class: 'btn btn-sm', type: 'button', text: '写答案', on: { click: write } }),
          h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '不用管', title: '不是资料该管的问题，不再列出',
            on: { click: function (e) {
              e.currentTarget.disabled = true;
              return api('PATCH', '/api/kb/gaps/' + encodeURIComponent(g.id), { status: 'ignored' }).then(function () {
                gone(); toast('不再列出这个问题。', 'info');
              }, function (ex) { view(); throw ex; });
            } } }),
          g.session_id ? h('a', { class: 'btn btn-ghost btn-sm', href: '#/task/' + encodeURIComponent(g.session_id), text: '去那次对话' }) : null)]);
    }
    function write() {
      clear(row);
      row.appendChild(h('div', { class: 'gap-q', text: g.question }));
      var ta = h('textarea', { rows: 3, placeholder: '例如：一线城市每晚不超过 800 元（《差旅管理办法（2026 版）》4.2）。', 'aria-label': '这个问题的答案' });
      var form = inlineForm([field('答案', ta, '存成标准答案后，下次有人这么问，Hspace 按它回答。')], '存成标准答案', function () {
        var t = ta.value.trim();
        if (!t) throw new Error('先写一下答案。');
        return api('POST', '/api/kb/gaps/' + encodeURIComponent(g.id) + '/answer', { answer: t }).then(function () {
          gone(); toast('存成标准答案了。', 'info');
          var ans = document.querySelector('.ans-list');
          if (ans && ans._reload) ans._reload();
        });
      });
      form.querySelector('.form-actions').appendChild(h('button', { class: 'btn btn-ghost', type: 'button', text: '算了', on: { click: view } }));
      form.addEventListener('keydown', function (e) { if (e.key === 'Escape') { e.preventDefault(); view(); } });
      row.appendChild(form);
      ta.focus();
    }
    view();
    return row;
  }

  /* ---------- 检索评测（第 4.3 节）：默认收起 ---------- */

  function evalSection(data, err) {
    var det = h('details', { class: 'panel eval' }, h('summary', { class: 'panel-title eval-sum' }, '检索评测',
      h('span', { class: 'eval-sum-sub', text: data.items.length ? data.items.length + ' 道题' + (data.runs[0] ? ' · 上次前 3 条命中 ' + evalPct(data.runs[0], 3) + '%' : '') : '' })));
    det.appendChild(h('p', { class: 'panel-desc', text: '一组"问题 → 应该找到的文件"。改了资料、换了向量模型之后跑一遍，看检索变好了还是变坏了。只测检索，不用模型。' }));
    var st = { items: data.items, runs: data.runs, last: null };
    var tableBox = h('div', { class: 'eval-items' });
    var resBox = h('div', { class: 'eval-res', 'aria-live': 'polite' });
    var kwOnly = h('input', { type: 'checkbox' });
    function reload() {
      return api('GET', '/api/kb/eval').then(function (d) { var n = normEval(d); st.items = n.items; st.runs = n.runs; drawItems(); });
    }
    function drawItems() {
      clear(tableBox);
      if (err && !st.items.length) { tableBox.appendChild(h('p', { class: 'form-error', text: (err && err.message) || '没有读到评测题。' })); return; }
      if (!st.items.length) { tableBox.appendChild(emptyRow('还没有题。点"加一题"，或者"从点过有用的回答里生成"。')); return; }
      var tb = h('tbody');
      st.items.forEach(function (it) {
        tb.appendChild(h('tr', {},
          h('td', { text: it.question }),
          h('td', { class: 'small' }, it.expect.map(function (x) { return h('span', { class: 'tag', text: x }); })),
          h('td', { class: 'act' }, h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '删', 'aria-label': '删掉这道题：' + clip(it.question, 30),
            on: { click: function () {
              return api('DELETE', '/api/kb/eval/' + encodeURIComponent(it.id)).then(function () {
                st.items = st.items.filter(function (x) { return x.id !== it.id; }); drawItems();
              });
            } } }))));
      });
      tableBox.appendChild(h('div', { class: 'table-wrap' }, h('table', { class: 'table eval-table' },
        h('thead', {}, h('tr', {}, h('th', { text: '问题' }), h('th', { text: '应该找到的文件' }), h('th', { class: 'act', text: '' }))), tb)));
    }
    function drawResult(run) {
      clear(resBox);
      if (!run) return;
      var prev = st.runs.filter(function (r) { return r.id !== run.id; })[0] || null;
      var cmp = evalCompare(run, prev);
      resBox.appendChild(h('div', { class: 'eval-score' }, cmp.map(function (c) {
        return h('div', { class: 'eval-cell' + (c.delta > 0 ? ' is-up' : c.delta < 0 ? ' is-down' : '') },
          h('span', { class: 'eval-k', text: '前 ' + c.k + ' 条命中' }),
          h('span', { class: 'eval-pct num', text: c.pct + '%' }),
          h('span', { class: 'eval-delta', text: deltaText(c.delta) || ' ' }));
      })));
      resBox.appendChild(h('p', { class: 'small muted', text: run.total + ' 道题 · ' + evalModeText(run.mode) + (run.ms != null ? ' · ' + groupNum(run.ms) + ' 毫秒' : '') +
        (run.ts ? ' · ' + formatTime(run.ts) : '') }));
      if (run.misses.length) {
        var ul = h('ul', { class: 'eval-miss' });
        run.misses.forEach(function (m) {
          ul.appendChild(h('li', {},
            h('div', { class: 'eval-mq', text: m.question }),
            h('div', { class: 'eval-mrow' }, h('span', { class: 'eval-ml', text: '应该是' }), h('span', { text: m.expect.join('、') || '—' })),
            h('div', { class: 'eval-mrow' }, h('span', { class: 'eval-ml', text: '实际是' }), h('span', { class: 'muted', text: m.got.length ? m.got.slice(0, 5).join('、') : '什么都没找到' }))));
        });
        resBox.appendChild(h('div', { class: 'kv-caption', text: '前 5 条里没找到应该找到的文件（' + run.misses.length + ' 题）' }));
        resBox.appendChild(ul);
      } else if (run.total && run.hasMisses) resBox.appendChild(h('p', { class: 'small', text: '每道题前 5 条里都找到了应该找到的文件。' }));
    }
    // 加一题
    var q = h('input', { type: 'text', placeholder: '例如：总监去上海住宿一晚最多多少？' });
    var ex = h('input', { type: 'text', placeholder: '例如：差旅管理办法（2026 版）' });
    var addForm = inlineForm([field('问题', q), field('应该找到的文件', ex, '写文件名就行，不用写扩展名。几份就用分号隔开。')], '加上', function () {
      var exp = parseExpect(ex.value);
      if (!q.value.trim() || !exp.length) throw new Error('问题和应该找到的文件都要写。');
      return api('POST', '/api/kb/eval', { question: q.value.trim(), expect: exp }).then(function () {
        q.value = ''; ex.value = ''; addForm.hidden = true; addBtn.hidden = false; addBtn.focus();
        return reload();
      });
    });
    addForm.hidden = true;
    addForm.addEventListener('keydown', function (e) { if (e.key === 'Escape') { e.preventDefault(); addForm.hidden = true; addBtn.hidden = false; addBtn.focus(); } });
    var addBtn = h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '加一题', on: { click: function () { addForm.hidden = false; addBtn.hidden = true; q.focus(); } } });
    var file = h('input', { type: 'file', accept: '.csv,.xlsx,text/csv', hidden: true });
    file.addEventListener('change', guard(function () {
      var f = file.files && file.files[0];
      file.value = '';
      if (!f) return;
      return fetch(apiUrl('/api/kb/eval/import', { name: f.name }), { method: 'POST', cache: 'no-store', body: f,
        headers: { 'X-Token': S.token, 'Content-Type': 'application/octet-stream' } }).then(function (r) {
        return r.text().then(function (txt) {
          var d = null; try { d = txt ? JSON.parse(txt) : null; } catch (e) { d = null; }
          if (!r.ok) throw new Error(apiErrorMessage(r.status, d));
          return d || {};
        });
      }, function () { throw new Error('连不上 Agent 程序，表格没有传上去。'); }).then(function (d) {
        // 真后端：{added, skipped: 数字, reasons: [{row, why}]}；row 是表里的行号（含表头）
        var skipped = Array.isArray(d.reasons) ? d.reasons : Array.isArray(d.skipped) ? d.skipped : [];
        var nSkip = typeof d.skipped === 'number' ? d.skipped : skipped.length;
        var why = skipped.slice(0, 3).map(function (x) {
          return x && typeof x === 'object' ? (x.row ? '第 ' + x.row + ' 行' + (x.why || x.reason ? '：' : '') : '') + str(x.why || x.reason) : str(x);
        }).filter(Boolean);
        toast('导入了 ' + (d.added != null ? d.added : d.imported != null ? d.imported : 0) + ' 道题' + (nSkip ? '，跳过 ' + nSkip + ' 道' + (why.length ? '：' + why.join('；') : '') : '') + '。', 'info');
        return reload();
      });
    }));
    var actions = h('div', { class: 'form-actions eval-acts' },
      addBtn,
      h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '导入…', title: '选一个表格：两列"问题""应命中的文件"，一格里几个文件用分号隔开',
        on: { click: function () { file.click(); } } }),
      h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '从点过有用的回答里生成', on: { click: function (e) {
        var b = e.currentTarget; b.disabled = true;
        return api('POST', '/api/kb/eval/seed', {}).then(function (d) {
          b.disabled = false;
          var n = d && (d.added != null ? d.added : d.count);
          toast(n ? '加了 ' + n + ' 道题。' : '没有能加的：还没有点过"有用"、并且引用了资料的回答，或者已经都加过了。', 'info');
          return reload();
        }, function (ex2) { b.disabled = false; throw ex2; });
      } } }),
      h('span', { class: 'eval-run' },
        h('label', { class: 'check check-inline' }, kwOnly, h('span', { text: '只用关键词' })),
        h('button', { class: 'btn btn-primary btn-sm', type: 'button', text: '跑一遍', on: { click: function (e) {
          if (!st.items.length) { toast('还没有题。先加几道。', 'info'); return; }
          var b = e.currentTarget; b.disabled = true; b.textContent = '正在跑…';
          clear(resBox);
          resBox.dataset.fresh = '1';
          return api('POST', '/api/kb/eval/run', { mode: kwOnly.checked ? 'keyword' : 'auto' }).then(function (d) {
            b.disabled = false; b.textContent = '跑一遍';
            var run = normEvalRun(d);
            drawResult(run);
            if (run) st.runs = [run].concat(st.runs.filter(function (r) { return r.id !== run.id; }));
          }, function (ex3) { b.disabled = false; b.textContent = '跑一遍'; throw ex3; });
        } } })));
    det.appendChild(tableBox);
    det.appendChild(actions);
    det.appendChild(file);
    det.appendChild(addForm);
    det.appendChild(resBox);
    drawItems();
    // 打开时显示上一次的结果；有逐题明细的接口（GET /api/kb/eval/runs/<id>）就补上没命中的题
    if (st.runs[0]) {
      drawResult(st.runs[0]);
      var last = st.runs[0];
      api('GET', '/api/kb/eval/runs/' + encodeURIComponent(last.id)).then(function (d) {
        var r = normEvalRun(d);
        if (!r || resBox.dataset.fresh) return;
        if (!r.hasMisses && d && d.detail && Array.isArray(d.detail.items)) {
          r.misses = d.detail.items.filter(function (x) { return x && !(x.rank > 0 && x.rank <= 5); }).map(function (x) {
            return { question: str(x.question), expect: strList(x.expect), got: strList(x.got) };
          });
          r.hasMisses = true;
        }
        if (r.hasMisses) { st.runs[0] = r; drawResult(r); }
      }, function () { /* 没有这个接口：只有分数 */ });
    }
    return det;
  }

  /* ---------- 一个资料库的详情 ---------- */

  var DOC_FILTERS = [['', '全部'], ['ok', '已收录'], ['failed', '没收进去'], ['skipped', '跳过了']];

  function pageLibrary(body, alive, libId) {
    var ui = { q: '', status: '', tag: '', open: {} };
    var docs = [], lib = null, lastDone = -1;
    function fetchDocs() {
      return api('GET', '/api/libraries/' + encodeURIComponent(libId) + '/docs').then(function (d) {
        docs = asList(d, 'docs').map(normDoc).filter(Boolean);
        docs.forEach(function (x) { if (x.id) S.docNames[x.id] = x.title || fileName(x.path); });
        DOCS_CACHE.byLib[libId] = { at: Date.now(), docs: docs };
      });
    }
    return Promise.all([loadLibrariesFresh(), fetchDocs(), loadShared()]).then(function () {
      if (!alive()) return;
      lib = (S.libs || []).filter(function (l) { return l.id === libId; })[0];
      clear(body);
      if (!lib) {
        body.appendChild(emptyRow('没有这个资料库，可能已经删掉了。'));
        body.appendChild(h('a', { class: 'btn', href: buildHash(S.mode, 'kb'), text: '回到知识库' }));
        return;
      }
      setText($('page-title'), lib.name);
      var sub = $('page-sub');
      setText(sub, lib.note || '');
      show(sub, !!lib.note);
      var ph = $('page-title');
      if (ph && ph.parentNode) ph.parentNode.insertBefore(h('nav', { class: 'crumb', 'aria-label': '位置' },
        h('a', { href: buildHash(S.mode, 'kb'), text: '知识库' }), h('span', { 'aria-hidden': 'true', text: ' / ' })), ph);

      // 状态和两个动作
      var stBox = h('div', { class: 'lib-status' });
      function drawStatus() {
        clear(stBox);
        var cur = (S.libs || []).filter(function (l) { return l.id === libId; })[0] || lib;
        lib = cur;
        var sl = sharedLibOf(cur);
        var cs = cardStatsText(cur.cards);
        append(stBox, [
          h('div', { class: 'lib-status-line' },
            !cur.enabled ? h('span', { class: 'lib-state tone-empty' }, h('span', { class: 'lib-dot', 'aria-hidden': 'true' }), '已停用') : libStatePill(cur),
            cur.shared ? h('span', { class: 'kb-badge tone-co', text: '公司' }) : null,
            cur.shared && (sl || cur.version) ? h('span', { class: 'lib-ver', text: sl ? sharedLibText(sl) : '第 ' + cur.version + ' 版' }) : null,
            h('span', { class: 'lib-count', text: groupNum(cur.docs) + ' 份资料 · ' + groupNum(cur.parts) + ' 段' + (cur.vectors ? ' · ' + groupNum(cur.vectors) + ' 段已算向量' : '') }),
            cs ? h('span', { class: 'lib-cards' + (cardsBusy(cur.cards) ? ' is-busy' : ''), title: '每份文件一张卡片：目录、摘要、标签。Hspace 问"这份文件讲什么"时用它', text: cs }) : null,
            langText(cur.lang) ? h('span', { class: 'lib-lang', text: langText(cur.lang) }) : null,
            syncedText(cur) ? h('span', { class: 'lib-synced', text: syncedText(cur) }) : null),
          progressBar(cur),
          cur.state === 'indexing' && cur.progress.current ? h('p', { class: 'lib-current mono', text: '正在读：' + cur.progress.current }) : null,
          cardsReason(cur.cards) && !cur.shared ? h('p', { class: 'small muted lib-cards-why', text: cardsReason(cur.cards) }) : null,
          sl && sl.error ? h('p', { class: 'small lib-shared-err', text: sl.error }) : null,
          sl && sl.vectors_why ? h('p', { class: 'small muted', text: '语义检索：' + sl.vectors_why }) : null,
          cur.tags.length ? tagFilterRow(cur.tags) : null,
          cur.lang === 'en' ? h('p', { class: 'small muted lib-lang-hint', text: '这个资料库以英文为主。用中文问时，Hspace 会提醒自己也用英文的说法查。' }) : null]);
      }
      function tagFilterRow(tags) {
        return h('div', { class: 'tag-filter', role: 'group', 'aria-label': '按标签筛文档' },
          h('span', { class: 'tag-filter-label', text: '标签' }),
          tags.map(function (t) {
            var on = ui.tag === t.tag;
            return h('button', { class: 'chip' + (on ? ' is-on' : ''), type: 'button', 'aria-pressed': on ? 'true' : 'false',
              title: on ? '再点一下不筛了' : '只看带这个标签的文档', on: { click: function () { ui.tag = on ? '' : t.tag; drawStatus(); drawDocs(); } } },
              t.tag, t.docs ? h('span', { class: 'chip-n', text: String(t.docs) }) : null);
          }));
      }
      drawStatus();
      var actions = lib.shared ? sharedLibActions(lib, function () { return loadLibrariesFresh().then(drawStatus); })
        : h('div', { class: 'form-actions lib-actions' },
        h('button', { class: 'btn', type: 'button', text: '重新收录', title: '把来源文件夹从头扫一遍：改过的重读，删掉的去掉', on: { click: function () {
          return api('POST', '/api/libraries/' + encodeURIComponent(libId) + '/reindex').then(function () {
            toast('开始重新收录「' + lib.name + '」。', 'info');
            return loadLibrariesFresh().then(drawStatus);
          });
        } } }),
        h('button', { class: 'btn btn-ghost btn-danger-text', type: 'button', text: '删除这个资料库',
          on: { click: confirmThen('删除资料库「' + lib.name + '」？\n只删索引，不动文件：文件夹里的文件都还在。', function () {
            return api('DELETE', '/api/libraries/' + encodeURIComponent(libId)).then(function () {
              S.scope = S.scope.filter(function (x) { return x !== libId; });
              toast('资料库已删除。文件夹里的文件没有动。', 'info');
              go('#/kb');
            });
          }) } }),
        exportButton(lib));
      var exportOut = h('div', { class: 'lib-export-out', 'aria-live': 'polite' });
      var top = h('section', { class: 'panel lib-top' }, stBox, actions, exportOut);
      body.appendChild(top);

      // 来源文件夹（公司共享的库没有：它由公司统一建好，在这台电脑上只读）
      var srcSec = section('来源', lib.shared ? '这是公司统一建好的资料库，从共享目录同步过来。这里不能加来源、不能重新收录，可以停用。'
        : '这个资料库收哪些文件夹或文件。程序只读，不改也不删。');
      var srcList = h('ul', { class: 'lib-sources' });
      lib.sources.forEach(function (s) {
        srcList.appendChild(h('li', {}, icon(IC_FOLDER, 14), h('span', { class: 'mono', text: s.path }),
          h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '去掉',
            on: { click: confirmThen('不再收录「' + s.path + '」？它的资料会从这个库里去掉，文件不动。', function () {
              return api('DELETE', '/api/libraries/' + encodeURIComponent(libId) + '/sources/' + encodeURIComponent(s.id)).then(function () { return renderPage('kb', libId); });
            }) } })));
      });
      if (!lib.sources.length && !lib.shared) srcList.appendChild(h('li', { class: 'muted', text: '还没有来源。' }));
      if (lib.shared) clear(srcList);
      srcSec.appendChild(srcList);
      var manual = h('input', { type: 'text', class: 'mono', placeholder: '文件夹或文件的路径' });
      var manualForm = inlineForm([field('路径', manual)], '加上', function () {
        var p = manual.value.trim();
        if (!p) throw new Error('请填写路径。');
        return addSource(p);
      });
      manualForm.hidden = !S.noPicker;
      function addSource(p) {
        return api('POST', '/api/libraries/' + encodeURIComponent(libId) + '/sources', { path: p }).then(function () {
          toast('加上了，正在收录。', 'info');
          return renderPage('kb', libId);
        });
      }
      srcSec.appendChild(h('div', { class: 'form-actions', hidden: lib.shared }, h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '添加文件夹…',
        hidden: S.noPicker, on: { click: function () {
          if (S.picking) return;
          S.picking = true;
          var note = pickNote('请在弹出的窗口里选择文件夹…');
          return api('POST', '/api/pick_folder', {}).then(function (d) {
            S.picking = false; note();
            if (!d || d.supported === false) { S.noPicker = true; manualForm.hidden = false; manual.focus(); return; }
            if (d.path) return addSource(str(d.path));
          }, function (e) { S.picking = false; note(); if (e && (e.missing || e.status === 404)) { S.noPicker = true; manualForm.hidden = false; return; } throw e; });
        } } })));
      if (lib.shared) manualForm.hidden = true;
      srcSec.appendChild(manualForm);
      body.appendChild(srcSec);

      // 文档表
      var docSec = section('文档', '');
      var q = h('input', { type: 'search', placeholder: docs.some(function (x) { return x.hasCard; }) ? '按名字或摘要筛' : '按名字筛', 'aria-label': '按名字筛文档' });
      var sel = h('select', { 'aria-label': '按状态筛' });
      DOC_FILTERS.forEach(function (f) { sel.appendChild(h('option', { value: f[0], text: f[1] })); });
      var count = h('span', { class: 'doc-count muted small', 'aria-live': 'polite' });
      var tableBox = h('div', { class: 'table-wrap' });
      q.addEventListener('input', function () { ui.q = q.value; drawDocs(); });
      sel.addEventListener('change', function () { ui.status = sel.value; drawDocs(); });
      docSec.appendChild(h('div', { class: 'doc-filter' }, q, sel, count));
      docSec.appendChild(tableBox);
      body.appendChild(docSec);

      function drawDocs() {
        var shown = filterDocs(docs, ui.q, ui.status, ui.tag);
        var failed = docs.filter(function (d) { return d && d.status === 'failed'; }).length;
        count.textContent = shown.length === docs.length ? '共 ' + docs.length + ' 份' + (failed ? '，' + failed + ' 份没收进去' : '')
          : '显示 ' + shown.length + ' / ' + docs.length + ' 份' + (ui.tag ? '（标签：' + ui.tag + '）' : '');
        clear(tableBox);
        if (!docs.length) { tableBox.appendChild(emptyRow(lib.state === 'indexing' ? '正在收录，文档会陆续出现。' : '这个资料库里还没有文档。')); return; }
        if (!shown.length) { tableBox.appendChild(emptyRow('没有符合条件的文档。')); return; }
        // 有文档卡片的新后端：点名字打开卡片，片段数那一格点了看切片；老后端照旧点名字看切片
        var cards = docs.some(function (x) { return x.hasCard; });
        var hasVer = docs.some(function (x) { return x.version_label; });
        var ncol = 4 + (hasVer ? 1 : 0);
        var tbl = h('table', { class: 'table doc-table' + (cards ? ' has-cards' : '') }, h('thead', {}, h('tr', {},
          h('th', { text: '文档' }), h('th', { text: '类型' }), hasVer ? h('th', { text: '版本' }) : null,
          h('th', { class: 'num', text: '片段' }), h('th', { text: '状态' }))));
        var tb = h('tbody');
        shown.slice(0, 500).forEach(function (d) {
          var id = str(d.id), open = !!ui.open[id], st = d.status || 'ok';
          var sumLine = summaryLine(d);
          var toggleParts = function () { ui.open[id] = !ui.open[id]; drawDocs(); };
          tb.appendChild(h('tr', { class: 'doc-row st-' + st + (open ? ' is-open' : '') },
            h('td', { class: 'doc-cell' },
              cards && st === 'ok'
                ? h('button', { class: 'doc-open', type: 'button', 'aria-haspopup': 'dialog', title: '打开文档卡片：摘要、目录、标签',
                    on: { click: function (e) { openDocCard(id, e.currentTarget, function (card) {
                      // 卡片里改了摘要或标签：表格里跟着变
                      d.summary = card.summary; d.summary_by = card.summary_by; d.tags = card.tags; d.card_state = card.card_state; drawDocs();
                    }); } } },
                    h('span', { class: 'doc-title', text: d.title || fileName(d.path) }))
                : h('button', { class: 'doc-open', type: 'button', 'aria-expanded': open ? 'true' : 'false', disabled: !d.parts,
                    title: d.parts ? '看它被切成了哪些片段' : null, on: { click: toggleParts } },
                    h('span', { class: 'doc-title', text: d.title || fileName(d.path) })),
              d.tags.length ? h('span', { class: 'doc-tags' }, d.tags.map(function (t) { return h('span', { class: 'tag', text: t }); })) : null,
              sumLine ? h('div', { class: 'doc-sum' }, d.summary_by === 'extract' ? h('span', { class: 'kb-badge', text: '开头摘录' }) : null, h('span', { text: sumLine }))
                : cards && st === 'ok' && d.card_state !== 'ready' ? h('div', { class: 'doc-sum is-wait', text: d.card_state === 'failed' ? '摘要没写成' : '摘要还在写' }) : null,
              h('div', { class: 'mono small muted doc-path', text: d.path }),
              st === 'failed' && d.error ? h('div', { class: 'doc-error', text: str(d.error) }) : null,
              st === 'skipped' && d.error ? h('div', { class: 'small muted', text: str(d.error) }) : null),
            h('td', { class: 'small', text: docKindText(d.kind) }),
            hasVer ? h('td', { class: 'small', text: d.version_label }) : null,
            h('td', { class: 'num mono' }, cards && d.parts
              ? h('button', { class: 'linklike num', type: 'button', 'aria-expanded': open ? 'true' : 'false', title: '看它被切成了哪些片段',
                  text: groupNum(d.parts), on: { click: toggleParts } })
              : (d.parts != null ? groupNum(d.parts) : '')),
            h('td', {}, h('span', { class: 'doc-st tone-' + (st === 'ok' ? 'ok' : st === 'failed' ? 'warn' : 'empty') },
              h('span', { class: 'lib-dot', 'aria-hidden': 'true' }), docStatusText(st)))));
          if (open) {
            var cell = h('td', { colspan: String(ncol) }, h('p', { class: 'muted small', text: '正在读取…' }));
            tb.appendChild(h('tr', { class: 'parts-row' }, cell));
            loadParts(id, cell);
          }
        });
        tbl.appendChild(tb);
        tableBox.appendChild(tbl);
        if (shown.length > 500) tableBox.appendChild(h('p', { class: 'muted small', text: '只列了前 500 份，用上面的筛选找。' }));
      }
      drawDocs();

      var cardsAt = 0, cardsTimer = null;
      S.kbLive = function (p) {
        if (!alive() || str(p.library_id) !== libId) return;
        drawStatus();
        if (p.__cards) {
          // 后台每写完一份摘要推一次：最多 3 秒重取一次文档表
          if (cardsTimer) return;
          cardsTimer = setTimeout(function () {
            cardsTimer = null; cardsAt = Date.now();
            fetchDocs().then(function () { if (alive()) drawDocs(); }, function () {});
          }, Math.max(0, 3000 - (Date.now() - cardsAt)));
          return;
        }
        if (p.done !== lastDone) { lastDone = p.done; fetchDocs().then(function () { if (alive()) drawDocs(); }, function () {}); }
      };
      pollWhile(alive, function () { return lib && lib.state === 'indexing'; }, function () {
        return Promise.all([loadLibrariesFresh(), fetchDocs()]).then(function () { if (alive()) { drawStatus(); drawDocs(); } });
      });
    });
  }

  /** 公司共享的库：只有"停用 / 启用"。 */
  function sharedLibActions(lib, after) {
    var sl = sharedLibOf(lib);
    // 共享目录里已经没有这个库了（或者不用共享目录了）：本机留着的这份可以删
    var orphan = (sl && sl.state === 'error' && /已经没有/.test(sl.error)) || (S.shared && !S.shared.missing && !S.shared.configured);
    return h('div', { class: 'form-actions lib-actions' },
      orphan ? h('button', { class: 'btn btn-ghost btn-danger-text', type: 'button', text: '删掉本机这份',
        on: { click: confirmThen('删掉本机留着的「' + lib.name + '」？公司已经不再提供它了。只删这台电脑上的索引。', function () {
          return api('DELETE', '/api/libraries/' + encodeURIComponent(lib.id)).then(function () { toast('删掉了。', 'info'); go('#/kb'); });
        }) } }) : null,
      h('button', { class: 'btn', type: 'button', text: lib.enabled ? '停用' : '启用',
        title: lib.enabled ? '这台电脑上先不查这个库（不删，随时能再启用）' : '重新开始查这个库',
        on: { click: function (e) {
          var b = e.currentTarget; b.disabled = true;
          var next = !lib.enabled;
          return api('PATCH', '/api/libraries/' + encodeURIComponent(lib.id), { enabled: next }).then(function () {
            lib.enabled = next;
            toast(next ? '「' + lib.name + '」启用了。' : '「' + lib.name + '」停用了。Hspace 先不查它。', 'info');
            return renderPage('kb', lib.id);
          }, function (ex) { b.disabled = false; throw ex; });
        } } }));
  }

  /** 自己建的库："导出给大家用"（写到公司共享目录）。共享目录这个功能没有时（老后端）不显示。 */
  function exportButton(lib) {
    if (!S.shared || S.shared.missing) return null;
    return h('button', { class: 'btn btn-ghost', type: 'button', text: '导出给大家用',
      title: S.shared.configured ? '把这个库的索引写到公司共享目录，别的电脑同步后就能直接用' : '先在 设置 → 公司共享目录 里填好共享目录',
      on: { click: function (e) {
        var b = e.currentTarget, out = b.closest('.lib-top') && b.closest('.lib-top').querySelector('.lib-export-out');
        if (!S.shared.configured) {
          toast('还没有设置公司共享目录。先到 设置 → 公司共享目录 填好路径。', 'info', { label: '去设置', run: function () { go('#/settings'); } });
          return;
        }
        b.disabled = true;
        b.textContent = '正在导出…';
        return api('POST', '/api/libraries/' + encodeURIComponent(lib.id) + '/export', {}).then(function (d) {
          b.disabled = false; b.textContent = '导出给大家用';
          d = d || {};
          var where = str(d.dir || d.path), ver = nonNeg(d.version || (d.manifest && d.manifest.version));
          if (out) {
            clear(out);
            out.appendChild(h('p', { class: 'lib-export-ok' }, h('span', { text: '导出好了' + (ver ? '：第 ' + ver + ' 版' : '') + '。' }),
              where ? h('span', { class: 'mono', text: where }) : null,
              h('span', { class: 'muted', text: '别的电脑下次同步时会拿到。' })));
          }
          loadShared(true);
        }, function (ex) { b.disabled = false; b.textContent = '导出给大家用'; throw ex; });
      } } });
  }

  /* ---------- 文档卡片（第 1 节）：右侧抽屉 ---------- */

  var DOCS_CACHE = { byLib: {}, tried: {} };   // 资料库编号 → {at, docs}，"#"选文件和"指定文件…"用
  var CARD = { el: null, back: null, docId: '', onChange: null, my: 0 };

  function cardDrawer() {
    var el = $('doc-card');
    if (!el || CARD.bound) return el;
    CARD.bound = true;
    el.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !e.isComposing) { e.preventDefault(); e.stopPropagation(); closeDocCard(); return; }
      trapTab(e, el);
    });
    var sc = $('card-scrim');
    if (sc) sc.addEventListener('click', function () { closeDocCard(); });
    return el;
  }

  function closeDocCard() {
    var el = $('doc-card');
    if (!el || el.hidden) return false;
    el.hidden = true;
    clear(el);
    CARD.docId = '';
    var sc = $('card-scrim');
    if (sc) sc.hidden = true;
    var b = CARD.back;
    CARD.back = null;
    if (b && document.contains(b)) { try { b.focus(); } catch (e) { /* 忽略 */ } }
    return true;
  }

  function openDocCard(docId, backTo, onChange) {
    var el = cardDrawer(), sc = $('card-scrim');
    if (!el) return;
    CARD.back = backTo || document.activeElement;
    CARD.docId = docId;
    CARD.onChange = onChange || null;
    var my = ++CARD.my;
    clear(el);
    el.appendChild(cardHead('文档卡片', null));
    el.appendChild(h('p', { class: 'muted small dc-pad', text: '正在读取…' }));
    el.hidden = false;
    if (sc) sc.hidden = false;
    var x = el.querySelector('.dc-close');
    if (x) x.focus();
    return api('GET', '/api/kb/docs/' + encodeURIComponent(docId) + '/card').then(function (d) {
      if (my !== CARD.my || el.hidden) return;
      var c = normCard(d);
      if (!c) throw new Error('没有读到这份文档的卡片。');
      drawCard(el, c);
    }, function (e) {
      if (my !== CARD.my || el.hidden) return;
      clear(el);
      el.appendChild(cardHead('文档卡片', null));
      el.appendChild(h('p', { class: 'form-error dc-pad', text: e && (e.status === 404 || e.status === 405) && /接口/.test(e.message || '')
        ? '程序还没有文档卡片功能。更新后再来。' : (e && e.message) || '没有读到这份文档的卡片。' }));
      var cl = el.querySelector('.dc-close'); if (cl) cl.focus();
    });
  }

  function cardHead(title, c) {
    return h('div', { class: 'dc-head' },
      h('div', { class: 'dc-titles' },
        h('h2', { class: 'dc-title', text: title }),
        c ? h('div', { class: 'dc-sub' }, [c.doc.library, docKindText(c.doc.kind), c.doc.version_label, langText(c.lang)].filter(Boolean).join(' · ')) : null),
      h('button', { class: 'toast-x dc-close', type: 'button', 'aria-label': '关闭文档卡片', title: '关闭（Esc）', text: '×', on: { click: function () { closeDocCard(); } } }));
  }

  function drawCard(el, c) {
    clear(el);
    // 公司共享的库：摘要和标签随公司的包来，这台电脑上不能改（后端会 400），入口都收起来
    var ro = (S.libs || []).some(function (l) { return l.id === c.doc.library_id && l.shared; });
    var changed = function (nc) { if (CARD.onChange) { try { CARD.onChange(nc); } catch (e) { /* 忽略 */ } } };
    function patch(body) {
      return api('PATCH', '/api/kb/docs/' + encodeURIComponent(c.doc.id), body).then(function (d) {
        var nc = normCard(d) || Object.assign({}, c, body, body.summary != null ? { summary_by: 'user' } : {});
        if (!nc.doc.id) nc.doc = c.doc;
        changed(nc);
        return nc;
      });
    }
    el.appendChild(cardHead(c.doc.title, c));
    var scroll = h('div', { class: 'dc-body' });
    el.appendChild(scroll);
    if (c.doc.path) scroll.appendChild(h('p', { class: 'mono small muted dc-path', text: c.doc.path }));
    if (ro) scroll.appendChild(h('p', { class: 'locked-note dc-ro', role: 'note' }, icon('lock', 13),
      h('span', { text: '公司共享的资料库：摘要和标签跟着公司的版本走，这里不能改。' })));
    scroll.appendChild(h('div', { class: 'dc-acts' },
      h('a', { class: 'btn btn-sm', href: kbFileUrl(c.doc.id), target: '_blank', rel: 'noopener', text: '打开原文件' }),
      ro ? null : h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '重新写摘要',
        title: c.summary_by === 'user' ? '你改过这份摘要。重新写会让模型按原文重写一份' : '让模型按原文重写一份摘要（在后台写，写好后这里会变）',
        on: { click: function (e) {
          if (c.summary_by === 'user' && !window.confirm('这份摘要你改过。重新写会换成模型写的，确定吗？')) return;
          var b = e.currentTarget; b.disabled = true;
          return api('POST', '/api/kb/docs/' + encodeURIComponent(c.doc.id) + '/card', { action: 'regenerate' }).then(function () {
            b.textContent = '已排进去了';
            toast('排进去了。后台写好后，再打开这张卡片就能看到。', 'info');
          }, function (ex) { b.disabled = false; throw ex; });
        } } })));

    // 摘要（可以改）
    var sumSec = h('section', { class: 'dc-sec' });
    function drawSummary() {
      clear(sumSec);
      var by = summaryByText(c.summary_by);
      sumSec.appendChild(h('div', { class: 'dc-cap' }, h('span', { text: '摘要' }), by ? h('span', { class: 'kb-badge', text: by }) : null,
        ro ? null : h('button', { class: 'linklike dc-edit', type: 'button', text: c.summary ? '改' : '写一段', on: { click: editSummary } })));
      if (c.summary) sumSec.appendChild(h('p', { class: 'dc-summary', text: c.summary }));
      var note = cardStateText(c);
      if (note) sumSec.appendChild(h('p', { class: 'small muted', text: note }));
      if (c.summary_by === 'extract' && c.card_state === 'ready') sumSec.appendChild(h('p', { class: 'small muted', text: '这是文件开头的一段原文。接上模型后会换成模型写的摘要。' }));
    }
    function editSummary() {
      clear(sumSec);
      var ta = h('textarea', { rows: 5, maxlength: 200, value: c.summary, 'aria-label': '摘要' });
      var left = h('span', { class: 'small muted num' });
      function cnt() { left.textContent = ta.value.length + ' / 200'; }
      ta.addEventListener('input', cnt);
      cnt();
      var form = inlineForm([field('摘要', ta, '写这是什么文件、管什么事、适用于谁、最要紧的几条。你改过的不会被自动覆盖。')], '保存', function () {
        var t = ta.value.trim();
        if (!t) throw new Error('摘要不能空着。');
        return patch({ summary: t }).then(function (nc) { c = nc; drawSummary(); toast('摘要改好了。', 'info'); });
      });
      form.querySelector('.form-actions').appendChild(left);
      form.querySelector('.form-actions').insertBefore(h('button', { class: 'btn btn-ghost', type: 'button', text: '算了', on: { click: function () { drawSummary(); } } }),
        form.querySelector('.form-actions').lastChild);
      form.addEventListener('keydown', function (e) { if (e.key === 'Escape' && !e.isComposing) { e.preventDefault(); e.stopPropagation(); drawSummary(); var b = sumSec.querySelector('.dc-edit'); if (b) b.focus(); } });
      sumSec.appendChild(form);
      ta.focus();
    }
    drawSummary();
    scroll.appendChild(sumSec);

    // 关键词
    if (c.keywords.length) {
      scroll.appendChild(h('section', { class: 'dc-sec' }, h('div', { class: 'dc-cap', text: '关键词' }),
        h('div', { class: 'dc-chips' }, c.keywords.map(function (k) { return h('span', { class: 'tag', text: k }); }))));
    }

    // 标签（可以加减）
    var tagSec = h('section', { class: 'dc-sec' });
    function drawTags(focusAdd) {
      clear(tagSec);
      tagSec.appendChild(h('div', { class: 'dc-cap' }, h('span', { text: '标签' }),
        ro ? null : h('span', { class: 'small muted', text: '用来筛文档、限定检索范围。最多 10 个。' })));
      var chips = h('div', { class: 'dc-chips' });
      if (ro) {
        c.tags.forEach(function (t) { chips.appendChild(h('span', { class: 'tag', text: t })); });
        if (!c.tags.length) chips.appendChild(h('span', { class: 'small muted', text: '没有标签' }));
        tagSec.appendChild(chips);
        return;
      }
      c.tags.forEach(function (t) {
        chips.appendChild(h('span', { class: 'tag tag-x' }, h('span', { text: t }),
          h('button', { class: 'tag-del', type: 'button', 'aria-label': '去掉标签「' + t + '」', title: '去掉', text: '×', on: { click: function () {
            return patch({ tags: removeTag(c.tags, t) }).then(function (nc) { c = nc; drawTags(true); });
          } } })));
      });
      var inp = h('input', { type: 'text', class: 'tag-input', maxlength: 20, placeholder: c.tags.length >= TAG_MAX ? '已经 10 个了' : '加一个标签，回车',
        'aria-label': '加一个标签', disabled: c.tags.length >= TAG_MAX });
      var err = h('span', { class: 'small form-error', role: 'alert', hidden: true });
      inp.addEventListener('keydown', guard(function (e) {
        if (e.key !== 'Enter' || e.isComposing || e.keyCode === 229) return;
        e.preventDefault();
        var r = addTag(c.tags, inp.value);
        if (r.error) { err.textContent = r.error; err.hidden = false; return; }
        if (r.tags.length === c.tags.length) return;
        inp.disabled = true;
        return patch({ tags: r.tags }).then(function (nc) { c = nc; drawTags(true); }, function (ex) { inp.disabled = false; throw ex; });
      }));
      inp.addEventListener('input', function () { err.hidden = true; });
      chips.appendChild(inp);
      tagSec.appendChild(chips);
      tagSec.appendChild(err);
      if (focusAdd) setTimeout(function () { if (!inp.disabled) inp.focus(); }, 0);
    }
    drawTags(false);
    scroll.appendChild(tagSec);

    // 目录：点一项看那一节的原文
    var olSec = h('section', { class: 'dc-sec' }, h('div', { class: 'dc-cap' }, h('span', { text: '目录' }),
      c.outline.length ? h('span', { class: 'small muted', text: '点一项看那一节的原文' }) : null));
    if (!c.outline.length) olSec.appendChild(h('p', { class: 'small muted', text: c.card_state === 'outline' || c.card_state === 'ready' ? '这份文件没有标题，取不出目录。' : '目录还没取出来。' }));
    else {
      var ol = h('ul', { class: 'dc-outline' });
      c.outline.forEach(function (o) {
        var box = h('div', { class: 'dc-sect', hidden: true });
        var btn = h('button', { class: 'dc-oi lv-' + o.level, type: 'button', 'aria-expanded': 'false', disabled: !o.section_id,
          title: o.section_id ? null : '这一项没有单独的一节',
          on: { click: function () {
            if (!box.hidden) { box.hidden = true; btn.setAttribute('aria-expanded', 'false'); return; }
            box.hidden = false; btn.setAttribute('aria-expanded', 'true');
            if (box.dataset.done) return;
            box.textContent = '正在读取…';
            return api('GET', '/api/kb/sections/' + encodeURIComponent(o.section_id)).then(function (d) {
              d = d || {};
              box.dataset.done = '1';
              box.textContent = str(d.text) || '这一节没有文字。';
            }, function (ex) { box.textContent = (ex && ex.message) || '没有读到这一节。'; });
          } } },
          h('span', { class: 'dc-ot', text: o.title }), o.loc && o.loc !== o.title ? h('span', { class: 'dc-ol', text: o.loc }) : null);
        ol.appendChild(h('li', {}, btn, box));
      });
      olSec.appendChild(ol);
    }
    scroll.appendChild(olSec);
    if (c.card_at) scroll.appendChild(h('p', { class: 'small muted dc-foot', text: '卡片更新于 ' + formatTime(c.card_at) }));
  }

  var partsCache = {};
  /** 切片预览：这份文档被切成了哪些片段（出处、标题路径、大小、原文）。 */
  function loadParts(docId, cell) {
    var p = partsCache[docId] ? Promise.resolve(partsCache[docId])
      : api('GET', '/api/kb/docs/' + encodeURIComponent(docId) + '/parts').then(function (d) { return (partsCache[docId] = asList(d, 'parts')); });
    return p.then(function (parts) {
      clear(cell);
      if (!parts.length) { cell.appendChild(h('p', { class: 'muted small', text: '这份文档没有切出片段。' })); return; }
      var limit = 40;
      var ol = h('ol', { class: 'parts' });
      function draw() {
        clear(ol);
        parts.slice(0, limit).forEach(function (x, i) {
          ol.appendChild(h('li', { class: 'part' },
            h('div', { class: 'part-head' },
              h('span', { class: 'part-n mono', text: String(x.seq != null ? x.seq : i + 1) }),
              h('span', { class: 'part-loc', text: str(x.loc) }),
              x.heading && x.heading !== x.loc ? h('span', { class: 'part-heading', text: str(x.heading) }) : null,
              x.tokens ? h('span', { class: 'part-tokens mono', text: groupNum(x.tokens) + ' 词元' }) : null),
            h('div', { class: 'part-text', text: clip(str(x.text), 600) })));
        });
      }
      draw();
      cell.appendChild(h('p', { class: 'muted small parts-sum', text: '切成了 ' + parts.length + ' 个片段。每个片段带着它的出处和标题，检索时命中的就是这些。' }));
      cell.appendChild(ol);
      if (parts.length > limit) {
        var more = h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '再显示 40 个', on: { click: function () {
          limit += 40; draw(); if (limit >= parts.length) more.hidden = true;
        } } });
        cell.appendChild(more);
      }
    }, function (e) {
      clear(cell);
      cell.appendChild(h('p', { class: 'form-error', text: (e && e.message) || '没有读到片段。' }));
    });
  }

  /* ---------------- 设置页：语义检索 ---------------- */

  function semanticSection(conf) {
    var emb = Object.assign({ base_url: '', model: '', api_key: '' }, conf.embedding || {});
    var rr = Object.assign({ base_url: '', model: '', api_key: '' }, conf.rerank || {});
    var kb = conf.kb || {};
    var caps = S.caps || {};
    var sec = section('语义检索', '接上内网的向量模型后，问法和资料里的写法不一样也能找到。不接也能用，只是退回关键词检索。');
    var g = gate('semantic_search');
    if (g.ok === false && (g.reason || g.fallback)) {
      sec.appendChild(h('div', { class: 'feature-off', role: 'note' },
        h('span', { class: 'feature-off-text', text: g.reason || '「语义检索」现在不能用。' }),
        g.fallback ? h('span', { class: 'feature-off-fallback', text: g.fallback }) : null));
    }
    function inp(obj, key, ph, cls, type) {
      return h('input', { type: type || 'text', class: cls || '', value: str(obj[key]), placeholder: ph, 'aria-label': ph,
        on: { input: function (ev) { obj[key] = ev.target.value; } } });
    }
    sec.appendChild(h('div', { class: 'kv-caption', text: '向量模型' }));
    sec.appendChild(h('div', { class: 'ep ep3' },
      field('地址', inp(emb, 'base_url', 'http://10.0.0.2:8001/v1', 'mono')),
      field('模型', inp(emb, 'model', 'Qwen/Qwen3-Embedding-0.6B', 'mono')),
      field('密钥', inp(emb, 'api_key', '没有可留空', 'mono', 'password'))));
    var testOut = h('div', { class: 'test-out' });
    sec.appendChild(h('div', { class: 'kv-caption', text: '重排模型（可不填）' }));
    sec.appendChild(h('div', { class: 'ep ep3' },
      field('地址', inp(rr, 'base_url', 'http://10.0.0.2:8002/v1', 'mono')),
      field('模型', inp(rr, 'model', 'Qwen/Qwen3-Reranker-0.6B', 'mono')),
      field('密钥', inp(rr, 'api_key', '没有可留空', 'mono', 'password'))));
    var topK = h('input', { type: 'number', min: '1', max: '20', value: String(kb.top_k || 5), class: 'num-input', 'aria-label': '每次检索取几条' });
    sec.appendChild(field('每次检索取几条', topK, '上下文小的本地模型少取几条。默认 5。'));
    var e = caps.embedding;
    var facts = h('dl', { class: 'about' },
      h('dt', { text: '向量库' }), h('dd', { text: e && e.kb_engine ? kbEngineText(e.kb_engine) : (kb.engine && kb.engine !== 'auto' ? kbEngineText(kb.engine) : '未知') }));
    if (e && e.model) append(facts, [h('dt', { text: '现在' }), h('dd', { text: embeddingLine(e) })]);
    sec.appendChild(facts);
    if (kb.engine === 'auto' || !kb.engine) sec.appendChild(h('p', { class: 'small muted', text: '装了 LangChain 那几个组件就用 Chroma，否则用内置的。换了向量模型后要在知识库里"重新收录"，旧的向量作废。' }));
    var oldModel = str(emb.model);
    sec.appendChild(h('div', { class: 'form-actions' },
      h('button', { class: 'btn btn-primary', type: 'button', text: '保存', on: { click: function () {
        var k = parseInt(topK.value, 10);
        if (!(k >= 1 && k <= 20)) throw new Error('每次检索取几条：填 1 到 20。');
        var body = {
          embedding: { engine: emb.base_url.trim() ? 'openai' : 'off', base_url: emb.base_url.trim(), model: emb.model.trim(), api_key: emb.api_key },
          rerank: { engine: rr.base_url.trim() ? 'vllm' : 'off', base_url: rr.base_url.trim(), model: rr.model.trim(), api_key: rr.api_key },
          kb: { top_k: k }
        };
        return api('POST', '/api/settings', body).then(function () {
          var changed = oldModel && oldModel !== emb.model.trim();
          oldModel = emb.model.trim();
          toast(changed ? '已保存。换了向量模型，到知识库里点"重新收录"才能用上。' : '已保存。', 'info');
          loadCaps();
        });
      } } }),
      h('button', { class: 'btn lock-ok', type: 'button', text: '测试', on: { click: function (ev) {
        var b = ev.currentTarget;
        b.disabled = true;
        clear(testOut);
        testOut.appendChild(h('p', { class: 'muted small', text: '正在算一个向量试试…' }));
        return api('POST', '/api/settings/test_embedding', { base_url: emb.base_url.trim(), model: emb.model.trim(), api_key: emb.api_key }).then(function (r) {
          b.disabled = false;
          clear(testOut);
          r = r || {};
          testOut.appendChild(h('div', { class: 'test-row ' + (r.ok ? 'is-ok' : 'is-bad') },
            h('span', { class: 'mono', text: emb.model || '向量模型' }),
            h('span', { class: 'test-state', text: r.ok ? '通了' : '不通' }),
            h('span', { class: 'small muted', text: r.ok ? [r.dims ? r.dims + ' 维' : '', typeof r.ms === 'number' ? '用时 ' + Math.round(r.ms) + ' 毫秒' : ''].filter(Boolean).join('，')
              : str(r.error || r.detail) })));
        }, function (ex) { b.disabled = false; clear(testOut); throw ex; });
      } } })));
    sec.appendChild(testOut);
    return sec;
  }

  /* ---------------- 新任务首页 ---------------- */

  function renderHome() {
    setText($('home-greet'), greeting(new Date().getHours()));
    // 问候下面那行和输入框提示：从 /api/state 的 modes 里取；老后端照旧
    var wm = findModeById(S.modes, S.mode);
    setText($('home-sub'), wm && wm.prompt_line ? wm.prompt_line : '想做什么？');
    var hin = $('home-input');
    if (hin) hin.placeholder = (wm && wm.placeholder ? wm.placeholder.replace(/[。.]\s*$/, '') : '问点什么，或者说一件要做的事') + '。输入 / 看指令';
    renderHomeKb();
    renderScopeChips('home');
    renderDeep();
    renderFolderTags();
    renderFiles('home');
    renderThumbs('home');
    renderTemplates();
    renderModeButtons();
    updateSendBtn('home');
    var input = $('home-input');
    if (input) setTimeout(function () { if (S.route === 'home') { try { input.focus(); } catch (e) { /* 忽略 */ } } }, 0);
  }

  function renderTemplates() {
    var roles = $('tpl-roles'), list = $('tpl-list'), box = $('templates'), sug = $('tpl-suggest'), full = $('tpl-full');
    if (!roles || !list || !sug) return;
    clear(roles); clear(list); clear(sug);
    if (!S.templates.length) { show(box, false); return; }
    show(box, true);
    if (S.tplRole >= S.templates.length) S.tplRole = 0;

    function fill(t) { return useTemplate('home', t); }

    // 输入框下面一行建议，最多 4 个；末尾"更多"展开按岗位的完整列表
    pickSuggestions(S.templates, 4).forEach(function (t) {
      var np = tplParams(t).length;
      sug.appendChild(h('button', { class: 'tpl-chip' + (np ? ' has-params' : ''), type: 'button', title: clip(t.prompt, 120) + (np ? '\n点了先填几个空' : ''),
        'aria-haspopup': np ? 'dialog' : null, on: { click: function () { fill(t); } } }, h('span', { text: t.title }),
        tplSourceText(t.source) ? h('span', { class: 'tpl-src', text: tplSourceText(t.source) }) : null));
    });
    sug.appendChild(h('button', { class: 'tpl-chip tpl-more', type: 'button', 'aria-expanded': S.tplMore ? 'true' : 'false',
      'aria-controls': 'tpl-full', text: S.tplMore ? '收起' : '更多',
      on: { click: function () {
        S.tplMore = !S.tplMore;
        renderTemplates();
        var b = $('tpl-suggest') && $('tpl-suggest').querySelector('.tpl-more');
        if (b) b.focus();
      } } }));
    show(full, S.tplMore);
    if (!S.tplMore) return;

    S.templates.forEach(function (g, i) {
      var sel = i === S.tplRole;
      roles.appendChild(h('button', { class: 'tpl-role', type: 'button', role: 'tab', 'aria-selected': sel ? 'true' : 'false',
        tabindex: sel ? '0' : '-1', text: g.role,
        on: {
          click: function () { S.tplRole = i; renderTemplates(); var b = $('tpl-roles').children[i]; if (b) b.focus(); },
          keydown: function (e) {
            var n = S.templates.length;
            if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
              e.preventDefault();
              S.tplRole = (S.tplRole + (e.key === 'ArrowRight' ? 1 : n - 1)) % n;
              renderTemplates();
              var b = $('tpl-roles').children[S.tplRole]; if (b) b.focus();
            }
          }
        } }));
    });
    S.templates[S.tplRole].items.forEach(function (t) {
      list.appendChild(h('button', { class: 'tpl', type: 'button', title: tplParams(t).length ? '先填几个空，再放进输入框' : '填入输入框', on: { click: function () { fill(t); } } },
        h('span', { class: 'tpl-title' }, h('span', { text: t.title }), tplSourceText(t.source) ? h('span', { class: 'tpl-src', text: tplSourceText(t.source) }) : null),
        h('span', { class: 'tpl-prompt', text: clip(t.prompt.replace(/\{\{\s*([^{}:：?？]*)[^{}]*\}\}/g, '「$1」'), 90) })));
    });
  }

  /* ---------- 带参数的模板（第 6、7.3 节）：输入框上方展开一个小表单 ---------- */

  /** 点了一个模板：没有空的照旧放进输入框；有空的弹表单，填好再放进去。都不自动发送。 */
  function useTemplate(which, t) {
    if (!t) return;
    var params = tplParams(t);
    if (!params.length) { closeTplForm(which); setInput(which, str(t.prompt)); return; }
    var values = {};
    params.forEach(function (p) { values[p.name] = p['default'] || ''; });
    S.tplForm[which] = { tpl: t, params: params, values: values, errors: {}, busy: false };
    drawTplForm(which, true);
  }

  function closeTplForm(which, focusBack) {
    var box = $(which + '-tplform');
    S.tplForm[which] = null;
    if (box) { box.hidden = true; clear(box); }
    if (focusBack) { var i = composerFor(which).input; if (i) try { i.focus(); } catch (e) { /* 忽略 */ } }
  }

  var tplFileCache = { ws: null, at: 0, names: [] };
  /** 当前文件夹里的文件名（第一层），给"file"类型的空做下拉提示。拿不到就是空的，照样能手填。 */
  function loadFolderFiles() {
    if (tplFileCache.ws === S.wsId && Date.now() - tplFileCache.at < 30000) return Promise.resolve(tplFileCache.names);
    return api('GET', apiUrl('/api/files', { workspace: S.wsId, path: '.' })).then(function (d) {
      var items = d && Array.isArray(d.items) ? d.items : [];
      tplFileCache = { ws: S.wsId, at: Date.now(), names: items.filter(function (x) { return x && !x.dir; }).map(function (x) { return str(x.name); }) };
      return tplFileCache.names;
    }, function () { return []; });
  }

  function tplControl(which, p, st, id) {
    var v = st.values[p.name] || '';
    var attrs = { id: id, 'aria-invalid': st.errors[p.name] ? 'true' : null, 'aria-describedby': st.errors[p.name] ? id + '-err' : null };
    var el;
    function onIn(e) { st.values[p.name] = e.target.value; if (st.errors[p.name]) { delete st.errors[p.name]; drawTplForm(which); var again = $(id); if (again) again.focus(); } }
    if (p.type === 'choice' || p.type === 'library') {
      var opts = p.type === 'choice' ? p.options : (S.libs || []).map(function (l) { return l.name; });
      el = h('select', attrs, h('option', { value: '', text: p.required ? '请选一个' : '不选' }), opts.map(function (o) { return h('option', { value: o, text: o }); }));
      el.value = v;
      el.addEventListener('change', onIn);
      return el;
    }
    var type = { month: 'month', date: 'date', number: 'number' }[p.type] || 'text';
    el = h('input', Object.assign(attrs, { type: type, value: v, autocomplete: 'off',
      placeholder: p.type === 'month' ? '例如 2026-10' : p.type === 'file' ? '文件夹里的文件名' : p.required ? '' : '可以不填' }));
    if (p.type === 'file') {
      var dl = h('datalist', { id: id + '-list' });
      el.setAttribute('list', id + '-list');
      loadFolderFiles().then(function (names) { names.slice(0, 200).forEach(function (n) { dl.appendChild(h('option', { value: n })); }); });
      el.addEventListener('input', function (e) { st.values[p.name] = e.target.value; });
      return [el, dl];
    }
    el.addEventListener('input', function (e) { st.values[p.name] = e.target.value; if (st.errors[p.name]) { delete st.errors[p.name]; var m = $(id + '-err'); if (m) m.hidden = true; el.removeAttribute('aria-invalid'); } });
    return el;
  }

  function drawTplForm(which, focusFirst) {
    var box = $(which + '-tplform'), st = S.tplForm[which];
    if (!box) return;
    if (!st) { box.hidden = true; clear(box); return; }
    clear(box);
    var form = h('form', { class: 'tf', autocomplete: 'off', 'aria-label': '填模板：' + st.tpl.title });
    form.appendChild(h('div', { class: 'tf-head' },
      h('span', { class: 'tf-title', text: st.tpl.title }),
      h('span', { class: 'tf-sub', text: '填几个空，放进输入框后可以再改' }),
      h('button', { class: 'toast-x', type: 'button', 'aria-label': '算了', title: '算了（Esc）', text: '×', on: { click: function () { closeTplForm(which, true); } } })));
    var grid = h('div', { class: 'tf-grid' });
    st.params.forEach(function (p, i) {
      var id = which + '-tf-' + i;
      grid.appendChild(h('label', { class: 'tf-label', 'for': id }, p.label, p.required ? null : h('span', { class: 'tf-opt', text: '可不填' })));
      grid.appendChild(h('div', { class: 'tf-ctl' + (st.errors[p.name] ? ' is-bad' : '') }, tplControl(which, p, st, id),
        h('span', { class: 'tf-err', id: id + '-err', role: 'alert', hidden: !st.errors[p.name],
          text: typeof st.errors[p.name] === 'string' ? st.errors[p.name] : '这一项要填' })));
    });
    form.appendChild(grid);
    var err = h('p', { class: 'form-error', role: 'alert', hidden: true });
    form.appendChild(err);
    form.appendChild(h('div', { class: 'tf-acts' },
      h('button', { class: 'btn btn-primary btn-sm', type: 'submit', disabled: st.busy, text: '放进输入框' }),
      h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '算了', on: { click: function () { closeTplForm(which, true); } } })));
    form.addEventListener('keydown', function (e) {
      if (e.isComposing || e.keyCode === 229) { if (e.key === 'Enter') e.stopPropagation(); return; }
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeTplForm(which, true); }
    });
    form.addEventListener('submit', guard(function (e) {
      e.preventDefault();
      var miss = tplMissing(st.params, st.values);
      if (miss.length) {
        st.errors = {};
        miss.forEach(function (n) { st.errors[n] = true; });
        drawTplForm(which);
        var bad = box.querySelector('[aria-invalid="true"]');
        if (bad) bad.focus();
        return;
      }
      st.busy = true;
      return renderTemplateText(st.tpl, st.values).then(function (text) {
        if (S.tplForm[which] !== st) return;
        closeTplForm(which);
        setInput(which, text);
      }, function (ex) {
        st.busy = false;
        // 后端 400：{error, missing: [名字], invalid: [{name, why}]}，标到对应的空上
        var dd = ex && ex.data ? ex.data : {};
        var m = Array.isArray(dd.missing) ? dd.missing.map(str) : [];
        var inv = Array.isArray(dd.invalid) ? dd.invalid.filter(function (x) { return x && x.name; }) : [];
        if (m.length || inv.length) {
          st.errors = {};
          m.forEach(function (n) { st.errors[n] = true; });
          inv.forEach(function (x) { st.errors[str(x.name)] = str(x.why) || '填得不对'; });
          drawTplForm(which);
          var bad = box.querySelector('[aria-invalid="true"]');
          if (bad) bad.focus();
          return;
        }
        err.textContent = (ex && ex.message) || '没有填进去，再试一次。';
        err.hidden = false;
      });
    }));
    box.appendChild(form);
    box.hidden = false;
    if (focusFirst) { var f = form.querySelector('input, select'); if (f) setTimeout(function () { try { f.focus(); } catch (x) { /* 忽略 */ } }, 0); }
  }

  /** 填好的值 → 文字。新后端用 POST /api/templates/render（以它为准）；没有这个接口或模板没有 id 时在本地填。 */
  function renderTemplateText(t, values) {
    var local = function () { return renderTpl(t.prompt, values); };
    if (!t.id || !t.source) return Promise.resolve(local());
    return api('POST', '/api/templates/render', { id: t.id, values: values }).then(function (d) {
      return d && typeof d.text === 'string' ? d.text : local();
    }, function (e) {
      if (e && (e.missing || e.status === 404 || e.status === 405)) return local();
      throw e;
    });
  }

  /* ---------------- 添加工作文件夹 ---------------- */

  function renderOnboard() {
    var first = !S.workspaces.length;
    var view = $('view-onboard');
    if (!view) return;
    var title = view.querySelector('h1');
    if (title) title.textContent = first ? '先选一个文件夹' : '添加文件夹';
    var err = $('onboard-error');
    if (err) err.hidden = true;
    var cancel = $('onboard-cancel');
    if (!first && !cancel) {
      var actions = view.querySelector('.form-actions');
      if (actions) actions.appendChild(h('button', { id: 'onboard-cancel', class: 'btn btn-ghost', type: 'button', text: '算了',
        on: { click: function () { go('#/new'); } } }));
    } else if (first && cancel) {
      cancel.parentNode.removeChild(cancel);
    }
    var name = $('onboard-name');
    if (name) setTimeout(function () { try { name.focus(); } catch (e) { /* 忽略 */ } }, 0);
  }

  function submitOnboard(e) {
    if (e) e.preventDefault();
    var name = $('onboard-name'), path = $('onboard-path'), err = $('onboard-error'), btn = $('onboard-submit');
    var n = name ? name.value.trim() : '', p = path ? path.value.trim() : '';
    if (!p) { if (err) { err.textContent = '请填写文件夹路径。'; err.hidden = false; } return; }
    if (!n) n = fileName(p) || '文件夹';
    if (btn) btn.disabled = true;
    return api('POST', '/api/workspaces', { name: n, path: p }).then(function (w) {
      if (btn) btn.disabled = false;
      w = w && w.workspace ? w.workspace : w;
      if (!w || !w.id) throw new Error('添加没有成功，请再试一次。');
      S.workspaces.push(w);
      if (name) name.value = '';
      if (path) path.value = '';
      setWorkspace(w.id);
    }, function (ex) {
      if (btn) btn.disabled = false;
      if (err) { err.textContent = (ex && ex.message) || '添加没有成功。'; err.hidden = false; }
    });
  }

  /* ---------------- 其他页面 ---------------- */

  var pageToken = 0;

  function renderPage(name, arg) {
    S.kbLive = null;
    var oldCrumb = document.querySelector('.page-head .crumb');
    if (oldCrumb) oldCrumb.parentNode.removeChild(oldCrumb);
    var meta = PAGES[name];
    setText($('page-title'), meta.title);
    var sub = $('page-sub');
    setText(sub, meta.sub);
    show(sub, !!meta.sub);
    var body = clear($('page-body'));
    if (!body) return;
    var my = ++pageToken;
    body.appendChild(h('p', { class: 'muted', text: '正在加载…' }));
    var fn = { kb: pageLibraries, memory: pageMemory, schedule: pageSchedule, skills: pageSkills, settings: pageSettings }[name];
    var r;
    try { r = fn(body, function () { return my === pageToken && S.route === 'page'; }, arg); } catch (e) { r = Promise.reject(e); }
    return Promise.resolve(r).then(null, function (e) {
      if (my !== pageToken) return;
      clear(body);
      body.appendChild(h('p', { class: 'form-error', role: 'alert', text: (e && e.message) || '加载失败。' }));
      body.appendChild(h('button', { class: 'btn', type: 'button', text: '重新加载', on: { click: function () { renderPage(name); } } }));
    });
  }

  function asList(d, key) {
    if (Array.isArray(d)) return d;
    if (d && Array.isArray(d[key])) return d[key];
    if (d && Array.isArray(d.items)) return d.items;
    return [];
  }

  function section(title, desc) {
    var s = h('section', { class: 'panel' }, h('h2', { class: 'panel-title', text: title }));
    if (desc) s.appendChild(h('p', { class: 'panel-desc', text: desc }));
    return s;
  }

  function emptyRow(text) { return h('p', { class: 'list-empty', text: text }); }

  function inlineForm(fields, submitLabel, onSubmit) {
    var err = h('p', { class: 'form-error', role: 'alert', hidden: true });
    var btn = h('button', { class: 'btn btn-primary', type: 'submit', text: submitLabel });
    var form = h('form', { class: 'form form-inline', autocomplete: 'off' }, fields, h('div', { class: 'form-actions' }, btn), err);
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      err.hidden = true;
      btn.disabled = true;
      var r;
      try { r = onSubmit(); } catch (ex) { r = Promise.reject(ex); }
      Promise.resolve(r).then(function () { btn.disabled = false; }, function (ex) {
        btn.disabled = false;
        err.textContent = (ex && ex.message) || '没有成功。';
        err.hidden = false;
      });
    });
    return form;
  }

  function field(label, control, help) {
    return h('label', { class: 'field' }, h('span', { class: 'field-label', text: label }), control,
      help ? h('span', { class: 'field-help', text: help }) : null);
  }

  function confirmThen(msg, fn) {
    return function () { if (window.confirm(msg)) return fn(); };
  }

  // 资料
  function pageKb(body, alive) {
    return api('GET', apiUrl('/api/kb', { workspace: S.wsId })).then(function (d) {
      if (!alive()) return;
      clear(body);
      var docs = asList(d, 'docs');
      var path = h('input', { type: 'text', class: 'mono', placeholder: '例如：制度/  或  合同/框架协议.pdf', required: true });

      var add = section('导入', '填文件夹里的文件或子文件夹路径。支持 Word、PDF、Excel、PPT、Markdown、文本。导入一个文件夹会包含里面所有文件。');
      add.appendChild(inlineForm([field('路径', path)], '导入', function () {
        var p = path.value.trim();
        if (!p) throw new Error('请填写路径。');
        return api('POST', '/api/kb', { workspace_id: S.wsId, path: p }).then(function (r) {
          r = r || {};
          var msg = '导入了 ' + (r.indexed != null ? r.indexed : '若干') + ' 个文件' + (r.chunks != null ? '，共 ' + r.chunks + ' 段。' : '。');
          if (r.failed && r.failed.length) msg += ' 有 ' + r.failed.length + ' 个没导入成功：' + r.failed.map(str).join('；');
          path.value = '';
          return pageKb(body, alive).then(function () { toast(msg, 'info'); });
        });
      }));

      body.appendChild(add);

      var list = section('已导入的文档', docs.length ? '共 ' + docs.length + ' 个。' : '');
      if (!docs.length) list.appendChild(emptyRow('还没有导入任何文档。'));
      else {
        var tbl = h('table', { class: 'table' }, h('thead', {}, h('tr', {},
          h('th', { text: '文档' }), h('th', { class: 'num', text: '段数' }), h('th', { text: '导入时间' }), h('th', { class: 'act', text: '' }))));
        var tb = h('tbody');
        docs.forEach(function (doc) {
          tb.appendChild(h('tr', {},
            h('td', {}, h('div', { text: doc.title || fileName(doc.path) }), h('div', { class: 'mono small muted', text: doc.path })),
            h('td', { class: 'num mono', text: doc.chunks != null ? doc.chunks : '' }),
            h('td', { class: 'small', text: formatTime(doc.added) }),
            h('td', { class: 'act' }, h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '移除',
              on: { click: confirmThen('从资料库移除「' + (doc.title || doc.path) + '」？原文件不会被删除。', function () {
                return api('DELETE', '/api/kb/' + encodeURIComponent(doc.id)).then(function () { return pageKb(body, alive); });
              }) } }))));
        });
        tbl.appendChild(tb);
        list.appendChild(h('div', { class: 'table-wrap' }, tbl));
      }
      body.appendChild(list);
    });
  }

  // 记忆
  var MEM_COLS = [
    { target: 'user', title: '关于我', sub: '所有文件夹通用', empty: '还没有。比如你的职务、负责的范围、报告喜欢的格式。',
      ph: '例如：报告先给结论，再给明细，不超过一页。' },
    { target: 'workspace', title: '这个文件夹的笔记', sub: '', empty: '还没有。比如文件放在哪、怎么命名、统计口径。',
      ph: '例如：月度对账表放在“对账”文件夹，文件名带年月。' }
  ];

  function pageMemory(body, alive) {
    var wsId = S.wsId;
    var logReq = api('GET', apiUrl('/api/memories/log', { workspace: wsId, limit: 50 })).then(
      function (d) { return { rows: normalizeLog(d) }; },
      function (e) { return { error: e }; });
    return api('GET', apiUrl('/api/memories', { workspace: wsId })).then(function (d) {
      var mem = normalizeMemories(d, wsId);
      // 旧版程序没有"最近的改动"，不用等它
      return (mem.legacy ? Promise.resolve({ rows: [] }) : logReq).then(function (log) {
        if (!alive() || wsId !== S.wsId) return;
        drawMemoryPage(body, alive, mem, log);
      });
    });
  }

  function drawMemoryPage(body, alive, mem, log) {
    clear(body);
    function reload() { return alive() ? pageMemory(body, alive) : null; }

    if (mem.legacy) {
      body.appendChild(h('p', { class: 'mem-hint', text: '现在运行的是旧版 Agent 程序：只能添加和删除，看不到用量和最近的改动。重新运行启动脚本后就有了。' }));
    }

    // 1. 自动记忆开关
    if (mem.settings) {
      var auto = section('自动记忆', '开着时，Agent 会在对话后自己整理值得记住的内容；关掉后只在你明说“记住”时才记。');
      if (!mem.settings.enabled) {
        auto.appendChild(h('p', { class: 'mem-hint', text: '记忆功能在配置文件里关掉了：Agent 现在不读也不写记忆。' }));
      }
      var box = h('input', { type: 'checkbox', checked: mem.settings.auto, disabled: !mem.settings.enabled });
      var autoErr = h('p', { class: 'form-error', role: 'alert', hidden: true });
      box.addEventListener('change', guard(function () {
        var want = box.checked;
        box.disabled = true;
        autoErr.hidden = true;
        return api('POST', '/api/settings', { memory: { auto: want } }).then(function () {
          box.disabled = false;
          mem.settings.auto = want;
          toast(want ? '自动记忆已打开。' : '自动记忆已关闭。以后只在你明说“记住”时才记。', 'info');
        }, function (e) {
          box.disabled = false;
          box.checked = !want;
          autoErr.textContent = (e && e.message) || '没有改成。';
          autoErr.hidden = false;
        });
      }));
      auto.appendChild(h('label', { class: 'check mem-auto' }, box, h('span', { text: '自动整理值得记住的内容' })));
      auto.appendChild(autoErr);
      body.appendChild(auto);
    }

    // 2. 两个本子
    var cols = h('div', { class: 'mem-cols' });
    MEM_COLS.forEach(function (c) {
      var w = ws();
      cols.appendChild(memColumn(c, mem[c.target], mem.usage[c.target], c.target === 'workspace' && w ? w.name : c.sub, reload, mem.legacy));
    });
    body.appendChild(h('section', { class: 'panel mem-panel', 'aria-label': '记忆内容' }, cols));

    // 3. 最近的改动
    if (!mem.legacy) body.appendChild(memLogSection(log, reload));
  }

  function memColumn(c, entries, usage, sub, reload, legacy) {
    var col = h('section', { class: 'mem-col', 'aria-label': c.title });
    var ut = usageText(usage);
    var head = h('div', { class: 'mem-head' },
      h('h2', { class: 'panel-title', text: c.title }),
      sub ? h('span', { class: 'mem-sub', text: sub }) : null);
    col.appendChild(head);
    if (ut) {
      var pct = usagePct(usage);
      var fill = h('span', { class: 'meter-fill' });
      fill.style.width = pct + '%';
      col.appendChild(h('div', { class: 'mem-usage' + (pct >= 80 ? ' is-high' : ''),
        title: usage.used != null && usage.budget != null ? '大约 ' + usage.used + ' / ' + usage.budget + '（按字数估算）' : null },
        h('span', { class: 'meter', role: 'img', 'aria-label': ut }, fill),
        h('span', { class: 'mem-usage-text mono', text: ut })));
    }
    var ul = h('ul', { class: 'rows mem-list' });
    entries.forEach(function (m) { ul.appendChild(memRow(m, reload, legacy)); });
    if (entries.length) col.appendChild(ul);
    else col.appendChild(emptyRow(c.empty));

    // 栏底的添加框：出错时（重复、相近、满了、被拒绝）把后端那句话显示在框下面
    var text = h('textarea', { rows: 2, placeholder: c.ph, 'aria-label': '添加到「' + c.title + '」' });
    var form = inlineForm([text], '添加', function () {
      var t = text.value.trim();
      if (!t) { text.focus(); throw new Error('请写下要记住的内容。'); }
      return api('POST', '/api/memories', { text: t, target: c.target, workspace_id: c.target === 'workspace' ? S.wsId : null })
        .then(function () { text.value = ''; return reload(); });
    });
    form.classList.add('mem-add');
    text.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && !e.isComposing) {
        e.preventDefault();
        if (form.requestSubmit) form.requestSubmit(); else form.dispatchEvent(new Event('submit', { cancelable: true }));
      }
    });
    col.appendChild(form);
    return col;
  }

  /** 一条记忆。legacy：旧版程序没有修改接口，只给删除。 */
  function memRow(m, reload, legacy) {
    var li = h('li', { class: 'row mem-row' });

    function meta() {
      var src = originLabel(m.origin);
      var date = shortDate(m.updated || m.created);
      var srcEl = m.session_id
        ? h('button', { class: 'linklike', type: 'button', text: src, title: '打开记下它的那个对话',
            on: { click: function () { go('#/task/' + encodeURIComponent(m.session_id)); } } })
        : h('span', { text: src });
      return h('div', { class: 'small muted mem-meta' }, srcEl, date ? h('span', { text: ' · ' + date }) : null);
    }

    function viewMode(focusEdit) {
      clear(li);
      var editBtn = legacy ? null : h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '修改', 'aria-label': '修改这条：' + clip(m.text, 30),
        on: { click: editMode } });
      li.appendChild(h('div', { class: 'row-main' }, h('div', { class: 'row-text', text: m.text }), meta()));
      li.appendChild(h('div', { class: 'row-actions' }, editBtn,
        h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '删除', 'aria-label': '删除这条：' + clip(m.text, 30),
          on: { click: confirmThen('删除这条记忆？\n\n' + clip(m.text, 120), function () {
            return api('DELETE', '/api/memories/' + encodeURIComponent(m.id)).then(function (r) {
              var logId = r && r.log_id != null ? r.log_id : null;
              toast('已删除。', 'info', logId == null ? null : { label: '撤销', run: function () {
                return undoMemory(logId, function (ok) { if (ok === false) toast(S.memUndo[str(logId)].msg); if (ok) reload(); });
              } });
              return reload();
            });
          }) } })));
      if (focusEdit && editBtn) editBtn.focus();
    }

    function editMode() {
      clear(li);
      var ta = h('textarea', { rows: 2, value: m.text, 'aria-label': '修改这条记忆' });
      var err = h('p', { class: 'form-error', role: 'alert', hidden: true });
      var save = h('button', { class: 'btn btn-primary btn-sm', type: 'button', text: '保存' });
      var cancel = h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '取消', on: { click: function () { viewMode(true); } } });
      function submit() {
        var t = ta.value.trim();
        if (!t) { err.textContent = '内容不能为空。不要这条的话，点取消再删除。'; err.hidden = false; return; }
        if (t === m.text) { viewMode(true); return; }
        save.disabled = true;
        err.hidden = true;
        return api('PATCH', '/api/memories/' + encodeURIComponent(m.id), { text: t }).then(function () {
          return reload();
        }, function (e) {
          save.disabled = false;
          err.textContent = (e && e.message) || '没有改成。';
          err.hidden = false;
          ta.focus();
        });
      }
      save.addEventListener('click', guard(submit));
      ta.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); viewMode(true); }
        else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && !e.isComposing) { e.preventDefault(); guard(submit)(); }
      });
      li.appendChild(h('div', { class: 'row-main mem-edit' }, ta,
        h('div', { class: 'form-actions' }, save, cancel, h('span', { class: 'muted small', text: 'Ctrl + 回车保存，Esc 取消' })), err));
      ta.focus();
    }

    viewMode(false);
    return li;
  }

  function memLogSection(log, reload) {
    var sec = section('最近的改动', '谁在什么时候改了记忆。点撤销可以改回去。');
    if (log.error) {
      sec.appendChild(emptyRow((log.error && log.error.message) || '没有读到改动记录。'));
      return sec;
    }
    var rows = log.rows || [];
    if (!rows.length) { sec.appendChild(emptyRow('还没有改动。')); return sec; }
    var ul = h('ul', { class: 'rows mem-log' });
    rows.forEach(function (r) {
      if (r.undone && r.id != null) S.memUndo[str(r.id)] = { state: 'done' };
      var u = S.memUndo[str(r.id)];
      var undone = r.undone || !!(u && u.state === 'done');
      var err = h('p', { class: 'form-error', role: 'alert', hidden: !(u && u.state === 'error'), text: u && u.state === 'error' ? u.msg : '' });
      var act = h('div', { class: 'row-actions' });
      if (undone) act.appendChild(h('span', { class: 'tag', text: '已撤销' }));
      else {
        var btn = h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '撤销', 'aria-label': '撤销这次' + r.opLabel + '：' + clip(r.text, 30) });
        btn.addEventListener('click', guard(function () {
          btn.disabled = true;
          err.hidden = true;
          return undoMemory(r.id, function (ok) {
            if (ok === true) { reload(); return; }
            if (ok === false) {
              btn.disabled = false;
              err.textContent = S.memUndo[str(r.id)].msg;
              err.hidden = false;
            }
          });
        }));
        act.appendChild(btn);
      }
      ul.appendChild(h('li', { class: 'row' + (undone ? ' is-off' : '') },
        h('div', { class: 'row-main' },
          h('div', { class: 'row-title' }, h('span', { class: 'tag', text: r.opLabel }), h('span', { class: 'tag', text: r.target })),
          h('div', { class: 'row-text' + (r.op === 'remove' ? ' is-removed' : ''), text: r.text }),
          r.before ? h('div', { class: 'small muted', text: '原来：' + r.before }) : null,
          h('div', { class: 'small muted' }, h('span', { text: r.origin + ' · ' + formatTime(r.ts) }),
            r.session_id ? h('button', { class: 'linklike', type: 'button', text: '  看那个对话',
              on: { click: function () { go('#/task/' + encodeURIComponent(r.session_id)); } } }) : null),
          err),
        act));
    });
    sec.appendChild(ul);
    return sec;
  }

  // 定时
  function pageSchedule(body, alive) {
    return api('GET', apiUrl('/api/schedules', { workspace: S.wsId })).then(function (d) {
      if (!alive()) return;
      clear(body);
      var items = asList(d, 'schedules');
      var prompt = h('textarea', { rows: 3, placeholder: '例如：汇总“销售日报”文件夹里本周的日报，生成一页周报 Word，存到“周报”文件夹。', required: true });
      var when = h('input', { type: 'text', placeholder: '每周五 17:00', required: true });
      var tomorrow = new Date(Date.now() + 86400000);
      var once = tomorrow.getFullYear() + '-' + pad(tomorrow.getMonth() + 1) + '-' + pad(tomorrow.getDate()) + ' 09:00';
      var chips = h('div', { class: 'chips' });
      ['每天 09:00', '每周一 09:00', '每2小时', once].forEach(function (x) {
        chips.appendChild(h('button', { class: 'chip mono', type: 'button', text: x, on: { click: function () { when.value = x; when.focus(); } } }));
      });
      var add = section('添加定时任务', '到点时 Agent 看不到现在的对话，所以把要做的事写完整：用哪些文件、做成什么、存到哪里。');
      add.appendChild(inlineForm([field('要做的事', prompt), field('什么时候', when, '可以写：每天 09:00、每周一 09:00、每30分钟、每2小时，或者某个具体时间。'), chips], '添加', function () {
        var p = prompt.value.trim(), w = when.value.trim();
        if (!p || !w) throw new Error('要做的事和时间都要填。');
        return api('POST', '/api/schedules', { workspace_id: S.wsId, prompt: p, when: w }).then(function () {
          prompt.value = ''; when.value = '';
          return pageSchedule(body, alive);
        });
      }));
      body.appendChild(add);

      var list = section('已有的定时任务');
      if (!items.length) list.appendChild(emptyRow('还没有。'));
      else {
        var ul = h('ul', { class: 'rows' });
        items.forEach(function (s) {
          var on = !!s.enabled;
          ul.appendChild(h('li', { class: 'row' + (on ? '' : ' is-off') },
            h('div', { class: 'row-main' },
              h('div', { class: 'row-title' }, h('span', { class: 'mono', text: s.spec || s.when || '' }),
                h('span', { class: 'tag', text: on ? '启用中' : '已停用' })),
              h('div', { class: 'row-text', text: s.prompt }),
              h('div', { class: 'small muted', text: (on && s.next_run ? '下次：' + formatTime(s.next_run) : '') +
                (s.last_run ? (on && s.next_run ? '，' : '') + '上次：' + formatTime(s.last_run) : '') })),
            h('div', { class: 'row-actions' },
              h('button', { class: 'btn btn-sm', type: 'button', text: on ? '停用' : '启用',
                on: { click: function () {
                  return api('PATCH', '/api/schedules/' + encodeURIComponent(s.id), { enabled: !on }).then(function () { return pageSchedule(body, alive); });
                } } }),
              h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '删除',
                on: { click: confirmThen('删除这个定时任务？', function () {
                  return api('DELETE', '/api/schedules/' + encodeURIComponent(s.id)).then(function () { return pageSchedule(body, alive); });
                }) } }))));
        });
        list.appendChild(ul);
      }
      body.appendChild(list);
    });
  }

  // 技能
  function pageSkills(body, alive) {
    return api('GET', '/api/skills').then(function (d) {
      if (!alive()) return;
      clear(body);
      var items = normalizeSkills(d);
      var mine = items.filter(function (s) { return s.source === 'user'; }).length;
      var list = section('可用的技能', '“内置”的随程序提供；“公司”的来自公司共享目录；“我的”是你同意后 Agent 存下的做法，可以删除。');
      if (!items.length) list.appendChild(emptyRow('没有找到技能。'));
      else {
        if (items.length > 1) list.appendChild(h('p', { class: 'muted small', text: '共 ' + items.length + ' 个' + (mine ? '，其中我的 ' + mine + ' 个。' : '。') }));
        var ul = h('ul', { class: 'rows' });
        items.forEach(function (s) { ul.appendChild(skillRow(s, function () { return alive() ? pageSkills(body, alive) : null; })); });
        list.appendChild(ul);
      }
      body.appendChild(list);
    });
  }

  function skillRow(s, reload) {
    var user = s.source === 'user';
    var open = false, loaded = false, loading = false;
    var bodyBox = h('div', { class: 'skill-body', hidden: true });
    var bodyId = 'skill-body-' + s.name.replace(/[^a-z0-9-]/gi, '_');
    bodyBox.id = bodyId;
    var toggle = h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '看正文', 'aria-expanded': 'false', 'aria-controls': bodyId,
      'aria-label': '看「' + s.name + '」的正文' });

    function setOpen(v) {
      open = v;
      bodyBox.hidden = !v;
      toggle.textContent = v ? '收起' : '看正文';
      toggle.setAttribute('aria-expanded', v ? 'true' : 'false');
    }
    toggle.addEventListener('click', guard(function () {
      if (open) { setOpen(false); return; }
      setOpen(true);
      if (loaded || loading) return;
      loading = true;
      clear(bodyBox);
      bodyBox.appendChild(h('p', { class: 'muted small', text: '正在读取…' }));
      return api('GET', '/api/skills/' + encodeURIComponent(s.name)).then(function (d) {
        loading = false;
        loaded = true;
        clear(bodyBox);
        var text = d && typeof d === 'object' ? str(d.body) : '';
        if (!text.trim()) { bodyBox.appendChild(h('p', { class: 'muted small', text: '这个技能没有正文。' })); return; }
        bodyBox.appendChild(setMarkdown(h('div', { class: 'md' }), text));
      }, function (e) {
        loading = false;
        clear(bodyBox);
        bodyBox.appendChild(h('p', { class: 'form-error', role: 'alert', text: (e && e.message) || '没有读到正文。' }));
      });
    }));

    var actions = h('div', { class: 'row-actions' }, toggle);
    if (user) {
      actions.appendChild(h('button', { class: 'btn btn-ghost btn-sm btn-danger-text', type: 'button', text: '删除',
        'aria-label': '删除技能「' + s.name + '」',
        on: { click: confirmThen('删除技能「' + s.name + '」？删除后 Agent 不会再照这个做法做事。', function () {
          return api('DELETE', '/api/skills/' + encodeURIComponent(s.name)).then(function () {
            toast('技能「' + s.name + '」已删除。', 'info');
            return reload();
          });
        }) } }));
    }
    return h('li', { class: 'row skill-row' },
      h('div', { class: 'row-main' },
        h('div', { class: 'row-title' }, h('span', { class: 'mono', text: s.name }),
          h('span', { class: 'tag' + (user ? ' tag-mine' : ''), text: user ? '我的' : s.source === 'shared' ? '公司' : '内置' })),
        h('div', { class: 'row-text', text: s.description || '' }),
        bodyBox),
      actions);
  }

  // 设置
  var SETTING_TEXT = { 'default': '按批准方式', allow: '直接做', ask: '先问我', deny: '不许用' };

  function pageSettings(body, alive) {
    var capsP = loadCaps().then(null, function () { return S.caps; });
    var tplP = api('GET', '/api/templates').then(function (x) { return Array.isArray(x) ? x : []; }, function () { return []; });
    return Promise.all([api('GET', '/api/settings'), capsP, tplP, loadShared(true)]).then(function (res) {
      var d = res[0];
      if (!alive()) return;
      clear(body);
      d = d || {};
      var conf = d.config || {};
      var tools = Array.isArray(d.tools) ? d.tools : [];
      // 第 5.3 节：被公司锁住的设置项（老后端没有 locked，什么都不锁）
      var locked = strList(d.locked);
      var tplAll = res[2] || [];

      // 1. 模型
      var eps = (Array.isArray(conf.endpoints) ? conf.endpoints : []).map(function (e) {
        return { name: str(e.name), base_url: str(e.base_url), model: str(e.model), api_key: str(e.api_key), internal: e.internal !== false };
      });
      // 0. 外观：第一组
      body.appendChild(themeSection());
      // 我的模板（新后端的模板带 source 时才有）
      if (tplAll.some(function (t) { return t && t.source; })) body.appendChild(myTemplatesSection(tplAll));
      // 技术项（模型地址、联网、内核）收进最后的"高级"，默认收起；还没填模型地址时自动展开
      var adv = h('details', { class: 'advanced', open: !(Array.isArray(conf.endpoints) && conf.endpoints.length) },
        h('summary', { text: '高级' }),
        h('p', { class: 'advanced-desc', text: '模型地址、联网和执行内核。一般由 IT 同事设置。' }));
      var model = section('模型服务', '公司内网的模型地址。可以填多个，一台不通时自动换另一台。');
      var epBox = h('div', { class: 'eps' });
      function drawEps() {
        clear(epBox);
        eps.forEach(function (e, i) {
          function inp(key, ph, cls, type) {
            return h('input', { type: type || 'text', class: cls || '', value: e[key], placeholder: ph, 'aria-label': ph,
              on: { input: function (ev) { e[key] = ev.target.value; } } });
          }
          epBox.appendChild(h('div', { class: 'ep' },
            field('名称', inp('name', '例如 GX10-A')),
            field('地址', inp('base_url', 'http://10.0.0.1:8000/v1', 'mono')),
            field('模型', inp('model', '模型名', 'mono')),
            field('密钥', inp('api_key', '没有可留空', 'mono', 'password')),
            h('div', { class: 'ep-actions' }, h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '去掉这一台',
              on: { click: function () { eps.splice(i, 1); drawEps(); } } }))));
        });
        if (!eps.length) epBox.appendChild(emptyRow('还没有模型地址，Agent 没法工作。'));
      }
      drawEps();
      model.appendChild(epBox);
      var testOut = h('div', { class: 'test-out' });
      model.appendChild(h('div', { class: 'form-actions' },
        h('button', { class: 'btn btn-ghost', type: 'button', text: '再加一台', on: { click: function () {
          eps.push({ name: '', base_url: '', model: eps[0] ? eps[0].model : '', api_key: '', internal: true }); drawEps();
        } } }),
        h('button', { class: 'btn btn-primary', type: 'button', text: '保存', on: { click: function () {
          var list = eps.filter(function (e) { return e.base_url.trim(); }).map(function (e) {
            return { name: e.name.trim() || e.base_url.trim(), base_url: e.base_url.trim(), model: e.model.trim(), api_key: e.api_key, internal: e.internal };
          });
          if (!list.length) throw new Error('至少要有一个模型地址。');
          return api('POST', '/api/settings', { endpoints: list }).then(function () { toast('模型服务已保存。', 'info'); loadCaps(); });
        } } }),
        h('button', { class: 'btn lock-ok', type: 'button', text: '测试连接', on: { click: function (ev) {
          var b = ev.currentTarget;
          b.disabled = true;
          clear(testOut);
          testOut.appendChild(h('p', { class: 'muted small', text: '正在测试，最多等几秒…' }));
          return api('POST', '/api/settings/test').then(function (res) {
            b.disabled = false;
            clear(testOut);
            var rows = Array.isArray(res) ? res : (res && typeof res === 'object' ? Object.keys(res).map(function (k) {
              var v = res[k]; return v && typeof v === 'object' ? Object.assign({ name: k }, v) : { name: k, ok: !!v, detail: str(v) };
            }) : []);
            if (!rows.length) testOut.appendChild(h('p', { class: 'muted small', text: '没有可测试的地址。' }));
            rows.forEach(function (r) {
              testOut.appendChild(h('div', { class: 'test-row ' + (r.ok ? 'is-ok' : 'is-bad') },
                h('span', { class: 'mono', text: r.name || '' }),
                h('span', { class: 'test-state', text: r.ok ? (r.model_found === false ? '通了，但没找到填写的模型名' : '通了') : '不通' }),
                h('span', { class: 'small muted', text: r.detail || '' })));
            });
          }, function (e) { b.disabled = false; clear(testOut); throw e; });
        } } })));
      model.appendChild(testOut);
      lockSection(model, locked, ['endpoints']);
      adv.appendChild(model);

      // 模型能力（老后端没有这个接口时不显示）
      if (S.caps && !S.caps.missing) adv.appendChild(capsSection());
      // 语义检索（新后端才有：设置里有 embedding，或者功能列表里有 semantic_search）
      if ((conf.embedding && typeof conf.embedding === 'object') || (S.caps && S.caps.byId && S.caps.byId.semantic_search)) adv.appendChild(lockSection(semanticSection(conf), locked, ['embedding', 'rerank', 'kb.top_k']));
      // 第 3.5、3.8、1.3 节：工具按需加载、重复调用检测、文档摘要
      if ((conf.tools && typeof conf.tools === 'object') || (conf.kb && 'cards' in conf.kb)) adv.appendChild(kernelExtSection(conf, locked));

      // 2. 批准方式
      var mode = section('批准方式', 'Agent 动手之前要不要先问你。删除文件不管选哪种都会先问。');
      var radios = h('div', { class: 'radios', role: 'radiogroup', 'aria-label': '批准方式' });
      MODES.forEach(function (m) {
        var r = h('input', { type: 'radio', name: 'approval_mode', value: m.id, checked: (conf.approval_mode || 'auto') === m.id,
          on: { change: function () { return setMode(m.id); } } });
        radios.appendChild(h('label', { class: 'radio' }, r, h('span', { class: 'radio-text' },
          h('span', { class: 'radio-label', text: m.label }), h('span', { class: 'radio-desc', text: m.desc }))));
      });
      mode.appendChild(radios);
      lockSection(mode, locked, ['approval_mode']);
      body.appendChild(mode);

      // 操作网页（新版后端才有：设置里有 browser，或者功能列表里有它）
      if ((conf.browser && typeof conf.browser === 'object') || (S.caps && S.caps.byId && S.caps.byId.browser)) {
        body.appendChild(lockSection(browserSection(conf.browser || {}), locked, ['browser.allowed_domains', 'browser.blocked_domains']));
      }

      // 所在地（新版后端才有：设置里有 user_location）
      if (typeof conf.user_location === 'string') body.appendChild(locationSection(conf));

      // 3. 当前工作文件夹
      var w = ws();
      if (w) {
        var wname = h('input', { type: 'text', value: w.name || '', maxlength: 60 });
        var instr = h('textarea', { rows: 4, value: w.instructions || '', placeholder: '例如：这里是华东区的经营数据。金额单位是万元。生成的文件放到“输出”文件夹。' });
        var egress = h('select', {},
          h('option', { value: 'intranet', text: '只走公司内网（推荐）' }),
          h('option', { value: 'open', text: '允许访问外部网络和外部模型' }));
        egress.value = w.egress === 'open' ? 'open' : 'intranet';
        var wsec = section(w.is_default ? '默认文件夹' : '当前文件夹', '这些说明每次都会告诉 Agent。也可以在文件夹里放一个 AGENT.md，效果一样。');
        wsec.appendChild(h('p', { class: 'mono small muted', text: w.path }));
        wsec.appendChild(inlineForm([field('名称', wname), field('给 Agent 的说明', instr), field('网络', egress)], '保存', function () {
          return api('PATCH', '/api/workspaces/' + encodeURIComponent(w.id), { name: wname.value.trim(), instructions: instr.value, egress: egress.value })
            .then(function (nw) {
              if (nw && nw.id) { for (var i = 0; i < S.workspaces.length; i++) if (S.workspaces[i].id === nw.id) S.workspaces[i] = nw; }
              renderWorkspace();
              toast('已保存。', 'info');
            });
        }));
        if (!w.is_default && w.id !== S.defaultWs) wsec.appendChild(h('div', { class: 'form-actions' }, h('button', { class: 'btn btn-ghost btn-sm btn-danger-text', type: 'button',
          text: '不再使用这个文件夹', on: { click: confirmThen('不再使用「' + w.name + '」？只是从列表里去掉，文件夹里的文件不会动。', function () {
            return api('DELETE', '/api/workspaces/' + encodeURIComponent(w.id)).then(function () {
              S.workspaces = S.workspaces.filter(function (x) { return x.id !== w.id; });
              S.wsId = null;
              return loadState().then(function () { go(S.workspaces.length ? '#/new' : '#/add'); });
            });
          }) } })));
        body.appendChild(wsec);
      }

      // 语音识别
      body.appendChild(lockSection(asrSection(conf.asr || {}), locked, ['asr']));

      // 4. 联网
      var web = conf.web || {};
      var webOn = h('input', { type: 'checkbox', checked: !!web.enabled });
      var searchUrl = h('input', { type: 'text', class: 'mono', value: web.search_url || '', placeholder: 'http://内网搜索服务/search' });
      var wsec2 = section('联网', '默认关闭。打开后 Agent 可以抓取网页、搜索（需要公司提供兼容 SearXNG 的搜索服务）。');
      wsec2.appendChild(inlineForm([
        h('label', { class: 'check' }, webOn, h('span', { text: '允许 Agent 联网' })),
        field('搜索服务地址', searchUrl)], '保存', function () {
        return api('POST', '/api/settings', { web: { enabled: webOn.checked, search_url: searchUrl.value.trim() } })
          .then(function () { toast('联网设置已保存。新对话开始生效。', 'info'); });
      }));
      lockSection(wsec2, locked, ['web']);
      adv.appendChild(wsec2);

      // 5. 每个工具
      var tsec = section('每个动作单独设置', '不改的话按上面的批准方式来。删除文件永远会先问。');
      if (!tools.length) tsec.appendChild(emptyRow('没有读到工具列表。'));
      else {
        var tbl = h('table', { class: 'table' }, h('thead', {}, h('tr', {},
          h('th', { text: '动作' }), h('th', { text: '影响' }), h('th', { text: '怎么处理' }))));
        var tb = h('tbody');
        tools.forEach(function (t) {
          var sel = h('select', { 'aria-label': toolInfo(t.name, {}).verb + ' 怎么处理' });
          ['default', 'allow', 'ask', 'deny'].forEach(function (k) { sel.appendChild(h('option', { value: k, text: SETTING_TEXT[k] })); });
          sel.value = SETTING_TEXT[t.setting] ? t.setting : 'default';
          var prev = sel.value;
          sel.addEventListener('change', guard(function () {
            var val = sel.value;
            return api('POST', '/api/settings/tool', { tool: t.name, setting: val }).then(function () {
              prev = val;
              toast('「' + toolInfo(t.name, {}).verb + '」改为' + SETTING_TEXT[val] + '。', 'info');
            }, function (e) { sel.value = prev; throw e; });
          }));
          tb.appendChild(h('tr', {},
            h('td', {}, h('div', { text: toolInfo(t.name, {}).verb }), h('div', { class: 'small muted' },
              h('span', { class: 'mono', text: t.name }), t.desc ? '  ' + clip(t.desc, 60) : '')),
            h('td', {}, h('span', { class: 'risk risk-' + (t.risk || 'unknown'), text: riskLabel(t.risk) })),
            h('td', {}, sel)));
        });
        tbl.appendChild(tb);
        tsec.appendChild(h('div', { class: 'table-wrap' }, tbl));
      }
      body.appendChild(tsec);

      // 6. 审计
      var asec = section('操作记录', '每个动作都有记录：什么时候、做了什么、谁批准的、结果如何。');
      var auditBox = h('div', {});
      asec.appendChild(h('div', { class: 'form-actions' }, h('button', { class: 'btn', type: 'button', text: '查看最近 100 条',
        on: { click: function () { return loadAudit(auditBox); } } })));
      asec.appendChild(auditBox);
      body.appendChild(asec);

      // 内核和版本；内核的提示（给开发者看的原话）只放在这里
      var ksec = section('执行内核', 'Agent 用哪套程序来安排步骤。');
      var about = h('dl', { class: 'about' },
        h('dt', { text: '内核' }), h('dd', { text: kernelLabel(S.info.kernel) }));
      if (S.info.version) append(about, [h('dt', { text: '版本' }), h('dd', { class: 'mono', text: str(S.info.version) })]);
      if (S.info.mock) append(about, [h('dt', { text: '模式' }), h('dd', { text: '演示模式，没有连接真实模型' })]);
      ksec.appendChild(about);
      if (S.info.kernel_notice) ksec.appendChild(h('p', { class: 'kernel-notice', text: str(S.info.kernel_notice) }));
      adv.appendChild(ksec);
      // 第 5 节：公司共享目录（老后端没有 /api/shared 时不显示）
      if (S.shared && !S.shared.missing) body.appendChild(sharedSection(conf.shared || {}, locked));
      body.appendChild(adv);
    });
  }

  /* ---------------- 设置页：被公司锁住的项、工具和摘要、公司共享目录（第 7.5 节） ---------------- */

  /** 这一节里有被锁住的键：整节置灰，标题旁写"由公司统一设置"。返回这一节（方便链式写）。 */
  function lockSection(sec, locked, keys) {
    if (!sec) return sec;
    var hit = [];
    keys.forEach(function (k) { lockedUnder(locked, k).forEach(function (x) { if (hit.indexOf(x) < 0) hit.push(x); }); });
    if (!hit.length) return sec;
    sec.classList.add('is-locked');
    // "测试"这类只读的按钮（带 lock-ok）照样能点
    var all = sec.querySelectorAll('input, select, textarea, button:not(.lock-ok)');
    for (var i = 0; i < all.length; i++) { all[i].disabled = true; all[i].setAttribute('aria-disabled', 'true'); }
    var which = hit.length > 1 || hit[0] !== keys[0] ? '：' + hit.map(settingKeyText).join('、') : '';
    var note = h('p', { class: 'locked-note', role: 'note' }, icon('lock', 13),
      h('span', { text: '由公司统一设置' + which + '。要改请找' + (S.shared && S.shared.contact ? S.shared.contact : ' IT 同事') + '。' }));
    var title = sec.querySelector('.panel-title');
    if (title && title.parentNode) title.parentNode.insertBefore(note, title.nextSibling);
    else sec.insertBefore(note, sec.firstChild);
    return sec;
  }

  function kernelExtSection(conf, locked) {
    var tc = conf.tools && typeof conf.tools === 'object' ? conf.tools : {};
    var kb = conf.kb && typeof conf.kb === 'object' ? conf.kb : {};
    var sec = section('工具和摘要', '给上下文小的本地模型省地方。一般不用改。');
    sec.classList.add('kext');
    var lazy = h('select', { 'aria-label': '工具按需加载' },
      h('option', { value: 'auto', text: '自动（模型的上下文小于 64K 时开）' }), h('option', { value: 'on', text: '开' }), h('option', { value: 'off', text: '关（每次都带上全部工具）' }));
    lazy.value = ['auto', 'on', 'off'].indexOf(str(tc.lazy)) >= 0 ? str(tc.lazy) : 'auto';
    var guardN = h('input', { type: 'number', min: '0', max: '10', class: 'num-input', value: String(typeof tc.loop_guard === 'number' ? tc.loop_guard : 3), 'aria-label': '重复几次就拦下' });
    var cards = h('select', { 'aria-label': '文档摘要' }, h('option', { value: 'auto', text: '自动（模型空闲时在后台写）' }), h('option', { value: 'off', text: '关（只有目录和开头摘录）' }));
    cards.value = str(kb.cards) === 'off' ? 'off' : 'auto';
    var fLazy = field('工具按需加载', lazy, '开着时一开始只带常用的几组工具，要用别的再加载。'),
        fGuard = field('重复调用检测', guardN, '同一个动作、同样的参数连续几次结果都一样，就拦下来让它换个做法。0 表示不拦。默认 3。'),
        fCards = field('文档摘要', cards, '每份资料一段摘要，用来回答"这份文件讲什么""哪些文件管审批"。');
    [[fLazy, 'tools.lazy'], [fGuard, 'tools.loop_guard'], [fCards, 'kb.cards']].forEach(function (x) {
      if (!isLocked(locked, x[1])) return;
      x[0].classList.add('is-locked');
      var c = x[0].querySelector('input, select'); if (c) c.disabled = true;
      x[0].appendChild(h('span', { class: 'locked-note is-inline' }, icon('lock', 12), h('span', { text: '由公司统一设置' })));
    });
    if (conf.tools) { sec.appendChild(fLazy); sec.appendChild(fGuard); }
    if ('cards' in kb) sec.appendChild(fCards);
    sec.appendChild(h('div', { class: 'form-actions' }, h('button', { class: 'btn btn-primary', type: 'button', text: '保存', on: { click: function () {
      var n = parseInt(guardN.value, 10);
      if (!(n >= 0 && n <= 10)) throw new Error('重复调用检测：填 0 到 10。');
      var body = {};
      if (conf.tools) {
        body.tools = {};
        if (!isLocked(locked, 'tools.lazy')) body.tools.lazy = lazy.value;
        if (!isLocked(locked, 'tools.loop_guard')) body.tools.loop_guard = n;
        if (!Object.keys(body.tools).length) delete body.tools;
      }
      if ('cards' in kb && !isLocked(locked, 'kb.cards')) body.kb = { cards: cards.value };
      if (!Object.keys(body).length) return;
      return api('POST', '/api/settings', body).then(function () { toast('已保存。新的一轮对话开始生效。', 'info'); });
    } } })));
    return sec;
  }

  /** 共享目录换了或检查过：公司的设置、技能、模板、资料库可能变了，重新取；设置页重画（锁住的项跟着变）。 */
  function afterSharedChange(quiet) {
    S.tplBy = {};
    SL.skills = null;
    loadTemplates();
    loadLibrariesFresh();
    syncTheme();
    loadState().then(null, function () {});
    if (!quiet && S.route === 'page' && $('page-title') && $('page-title').textContent === PAGES.settings.title) {
      var y = window.scrollY || 0;
      Promise.resolve(renderPage('settings')).then(function () { try { window.scrollTo(0, y); } catch (e) { /* 忽略 */ } });
    }
  }

  function sharedSection(sconf, locked) {
    var sec = section('公司共享目录', '公司在共享盘上放一个文件夹：统一的设置、技能、模板和建好的资料库。填上它的路径，这台电脑就跟着公司的走。');
    var dir = h('input', { type: 'text', class: 'mono', value: str(sconf.dir) || (S.shared && S.shared.dir) || '', placeholder: '例如：\\\\fileserver\\CompanyAgent  或  /Volumes/共享盘/CompanyAgent',
      'aria-label': '共享目录的路径' });
    var m0 = typeof sconf.sync_minutes === 'number' ? sconf.sync_minutes : S.shared && S.shared.sync_minutes != null ? S.shared.sync_minutes : 30;
    var mins = h('input', { type: 'number', min: '0', max: '1440', class: 'num-input', value: String(m0), 'aria-label': '多久看一次更新（分钟）' });
    var out = h('div', { class: 'shared-out', 'aria-live': 'polite' });
    function drawState() {
      clear(out);
      var sh = S.shared || {};
      var stt = sharedStateText(sh);
      out.appendChild(h('div', { class: 'shared-state' },
        h('span', { class: 'lib-state tone-' + stt.tone }, h('span', { class: 'lib-dot', 'aria-hidden': 'true' }), stt.text),
        sh.checked ? h('span', { class: 'small muted', text: '上次检查 ' + relativeTime(sh.checked) }) : null,
        sh.synced && sh.ok ? h('span', { class: 'small muted', text: '上次同步 ' + relativeTime(sh.synced) }) : null));
      if (!sh.configured) return;
      var facts = h('dl', { class: 'about' });
      if (sh.name) append(facts, [h('dt', { text: '公司' }), h('dd', { text: sh.name })]);
      if (sh.contact) append(facts, [h('dt', { text: '有事找' }), h('dd', { text: sh.contact })]);
      append(facts, [h('dt', { text: '内容' }), h('dd', { text: [sh.skills + ' 个技能', sh.templates + ' 条模板', sh.libraries.length + ' 个资料库'].join(' · ') })]);
      if (sh.locked.length) append(facts, [h('dt', { text: '公司定死的' }), h('dd', { text: sh.locked.map(settingKeyText).join('、') })]);
      if (sh.defaults.length) append(facts, [h('dt', { text: '公司给的默认' }), h('dd', { text: sh.defaults.map(settingKeyText).join('、') + '（这台电脑改过的以这台为准）' })]);
      out.appendChild(facts);
      if (sh.libraries.length) {
        var tb = h('tbody');
        sh.libraries.forEach(function (l) {
          tb.appendChild(h('tr', {},
            h('td', { text: l.name }),
            h('td', { class: 'small', text: sharedLibText(l) }),
            h('td', { class: 'num mono', text: groupNum(l.docs) + ' 份' }),
            h('td', { class: 'small' + (l.vectors === 'mismatch' ? ' is-warn' : '') },
              h('span', { text: VECTORS_TEXT[l.vectors] || '' }),
              l.vectors_why ? h('span', { class: 'shared-why', text: l.vectors_why }) : null,
              l.error ? h('span', { class: 'shared-why is-err', text: l.error }) : null,
              !l.enabled ? h('span', { class: 'shared-why', text: '这台电脑上停用了' }) : null)));
        });
        out.appendChild(h('div', { class: 'table-wrap' }, h('table', { class: 'table shared-libs' },
          h('thead', {}, h('tr', {}, h('th', { text: '资料库' }), h('th', { text: '版本' }), h('th', { class: 'num', text: '文件' }), h('th', { text: '检索' }))), tb)));
      }
      // 连不上时状态那一行已经说了"用的是上次同步的内容"，notes 里同样意思的那句不再重复
      sh.notes.filter(function (n) { return sh.ok || !/^连不上共享目录/.test(n); }).forEach(function (n) { out.appendChild(h('div', { class: 'feature-off shared-note', role: 'note' }, h('span', { class: 'feature-off-text', text: n }))); });
      if (sh.ignored.length) {
        out.appendChild(h('div', { class: 'kv-caption', text: '没有用上的公司设置' }));
        var ul = h('ul', { class: 'shared-ignored' });
        sh.ignored.forEach(function (x) { ul.appendChild(h('li', {}, h('span', { class: 'mono', text: x.key }), h('span', { class: 'muted', text: x.why || '不在可以下发的范围里' }))); });
        out.appendChild(ul);
      }
    }
    drawState();
    sec.appendChild(h('div', { class: 'shared-row' }, field('路径', dir, '只读的共享文件夹最好。空着就是不用，一切照旧。'),
      field('多久看一次更新', h('span', { class: 'inline-num' }, mins, h('span', { class: 'muted small', text: '分钟' })), '填 0 就只在打开程序和点"检查"时看。')));
    sec.appendChild(h('div', { class: 'form-actions' },
      h('button', { class: 'btn btn-primary', type: 'button', text: '保存并检查', on: { click: function (e) {
        var n = parseInt(mins.value, 10);
        if (!(n === 0 || (n >= 5 && n <= 1440))) throw new Error('多久看一次更新：填 5 到 1440 分钟，或者填 0（只在打开程序和点"检查"时看）。');
        var b = e.currentTarget; b.disabled = true;
        return api('POST', '/api/settings', { shared: { dir: dir.value.trim(), sync_minutes: n } }).then(function () {
          return loadShared('check');
        }).then(function () {
          b.disabled = false; drawState();
          toast(!dir.value.trim() ? '不用共享目录了。' : S.shared && S.shared.ok ? '连上了共享目录。' : '保存了，但现在连不上这个目录。看看路径对不对、共享盘有没有挂上。', 'info');
          afterSharedChange();
        }, function (ex) { b.disabled = false; throw ex; });
      } } }),
      h('button', { class: 'btn', type: 'button', text: '检查', title: '马上看一遍共享目录有没有更新', on: { click: function (e) {
        var b = e.currentTarget; b.disabled = true;
        return loadShared('check').then(function () { b.disabled = false; drawState(); afterSharedChange(true); }, function (ex) { b.disabled = false; throw ex; });
      } } })));
    sec.appendChild(out);
    return lockSection(sec, locked, ['shared']);
  }

  /* ---------------- 设置页：我的模板（第 7.3 节） ---------------- */

  var TPL_PARSE = { missing: false };

  /** 本机的模板：列出、新建、改、删。GET /api/templates 的每项带 source（新后端）时才显示。 */
  function myTemplatesSection(all) {
    var sec = section('我的模板', '常用的说法存成模板，新对话页和 / 指令里都能点。用 {{月份:month}} 这样的写法留空，点了先填空再放进输入框。');
    var box = h('div', { class: 'mytpl-list' });
    var rows = all.filter(function (t) { return t && str(t.source) === 'user'; });
    function reload() {
      return api('GET', '/api/templates').then(function (d) {
        rows = (Array.isArray(d) ? d : []).filter(function (t) { return t && str(t.source) === 'user'; });
        S.tplBy = {};
        loadTemplates();
        draw();
      });
    }
    function draw() {
      clear(box);
      if (!rows.length) box.appendChild(emptyRow('还没有。点"新建模板"，写一段常说的话。'));
      rows.forEach(function (t) { box.appendChild(row(t)); });
    }
    function row(t) {
      var el = h('div', { class: 'row mytpl' });
      var ps = tplParams(t);
      append(el, [h('div', { class: 'row-main' },
          h('div', { class: 'row-title' }, h('span', { text: str(t.title) || '没起名字' }),
            t.mode ? h('span', { class: 'tag', text: modeName(str(t.mode)) || str(t.mode) }) : null,
            t.role ? h('span', { class: 'tag', text: str(t.role) }) : null),
          h('div', { class: 'row-text small', text: clip(str(t.prompt), 160) }),
          ps.length ? h('div', { class: 'small muted', text: '要填：' + ps.map(function (p) { return p.label; }).join('、') }) : null),
        h('div', { class: 'row-actions' },
          h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '改', on: { click: function () { el.parentNode.replaceChild(editor(t), el); } } }),
          h('button', { class: 'btn btn-ghost btn-sm btn-danger-text', type: 'button', text: '删',
            on: { click: confirmThen('删掉模板「' + (str(t.title) || '没起名字') + '」？', function () {
              return api('DELETE', '/api/templates/' + encodeURIComponent(t.id)).then(reload);
            }) } }))]);
      return el;
    }
    function editor(t) {
      var title = h('input', { type: 'text', maxlength: 40, value: t ? str(t.title) : '', placeholder: '例如：月度经营分析' });
      var prompt = h('textarea', { rows: 4, value: t ? str(t.prompt) : '', placeholder: '例如：分析 {{月份:month}} 的经营数据，重点看{{部门:choice=销售|市场|财务}}，{{备注?}}' });
      var role = h('input', { type: 'text', maxlength: 20, value: t ? str(t.role) : '', placeholder: '例如：财务（不填归到"通用"）' });
      var mode = h('select', {}, h('option', { value: '', text: '两种模式都用' }),
        (S.modes || []).map(function (m) { return h('option', { value: m.id, text: m.name }); }));
      mode.value = t ? str(t.mode) : '';
      var live = h('div', { class: 'tpl-live', 'aria-live': 'polite' });
      function preview() {
        clear(live);
        var ps = parseTplParams(prompt.value), bad = tplSlotProblems(prompt.value);
        if (!ps.length && !bad.length) live.appendChild(h('span', { class: 'muted', text: '没有要填的空：点了直接放进输入框。' }));
        else if (ps.length) live.appendChild(h('span', {}, h('span', { class: 'muted', text: '解析出 ' + ps.length + ' 个空：' }),
          tplParamsText(ps).map(function (x) { return h('span', { class: 'tag', text: x }); })));
        if (bad.length) live.appendChild(h('span', { class: 'tpl-bad', text: '这几处写法不对，会当普通文字：' + bad.join(' ') +
          '。类型只能是 text、month、date、number、file、library、choice；choice 要写选项，例如 {{部门:choice=销售|市场}}' }));
      }
      // 新后端有 POST /api/templates/parse：以它的解析和报错为准（停顿 300 毫秒再问）；没有就用本地的
      var ptimer = null, pseq = 0;
      function remote() {
        if (TPL_PARSE.missing) return;
        clearTimeout(ptimer);
        ptimer = setTimeout(function () {
          var me = ++pseq, text = prompt.value;
          api('POST', '/api/templates/parse', { prompt: text }).then(function (d) {
            if (me !== pseq || !d) return;
            var ps = normTplParams(d.params), errs = strList(d.errors);
            clear(live);
            if (!ps.length && !errs.length) live.appendChild(h('span', { class: 'muted', text: '没有要填的空：点了直接放进输入框。' }));
            else if (ps.length) live.appendChild(h('span', {}, h('span', { class: 'muted', text: '解析出 ' + ps.length + ' 个空：' }),
              tplParamsText(ps).map(function (x) { return h('span', { class: 'tag', text: x }); })));
            errs.forEach(function (e) { live.appendChild(h('span', { class: 'tpl-bad', text: e })); });
          }, function (e) { if (e && (e.missing || e.status === 404 || e.status === 405)) TPL_PARSE.missing = true; });
        }, 300);
      }
      prompt.addEventListener('input', function () { preview(); remote(); });
      preview();
      remote();
      var form = inlineForm([field('名字', title), field('要说的话', prompt), live, h('div', { class: 'ep ep2' }, field('分组', role), field('用在', mode))],
        t ? '保存' : '存成模板', function () {
          var body = { title: title.value.trim(), prompt: prompt.value.trim(), role: role.value.trim() || undefined, mode: mode.value || undefined };
          if (!body.title || !body.prompt) throw new Error('名字和要说的话都要写。');
          var req = t ? api('PATCH', '/api/templates/' + encodeURIComponent(t.id), body) : api('POST', '/api/templates', body);
          return req.then(function () { toast(t ? '模板改好了。' : '存好了。新对话页和 / 指令里都能找到它。', 'info'); return reload().then(function () { addBtn.hidden = false; }); });
        });
      form.classList.add('mytpl-edit');
      form.querySelector('.form-actions').appendChild(h('button', { class: 'btn btn-ghost', type: 'button', text: '算了', on: { click: function () { draw(); addBtn.hidden = false; addBtn.focus(); } } }));
      form.addEventListener('keydown', function (e) { if (e.key === 'Escape' && !e.isComposing) { e.preventDefault(); e.stopPropagation(); draw(); addBtn.hidden = false; addBtn.focus(); } });
      setTimeout(function () { title.focus(); }, 0);
      return form;
    }
    var addBtn = h('button', { class: 'btn btn-sm', type: 'button', text: '新建模板', on: { click: function () {
      addBtn.hidden = true;
      box.insertBefore(editor(null), box.firstChild);
      var empty = box.querySelector('.list-empty'); if (empty) empty.parentNode.removeChild(empty);
    } } });
    var head = h('div', { class: 'panel-head' });
    var ttl = sec.querySelector('.panel-title');
    sec.insertBefore(head, ttl);
    head.appendChild(ttl);
    head.appendChild(addBtn);
    draw();
    sec.appendChild(box);
    return sec;
  }

  /** 设置页"所在地"：填了就用填的；空着时按电脑时区推断，推断的结果写在下面。 */
  function locationSection(conf) {
    var sec = section('所在地', '查天气、看附近、算时差这类事要用。空着时按这台电脑的时区推断。');
    var saved = str(conf.user_location);
    var inp = h('input', { type: 'text', value: saved, maxlength: '40', placeholder: '例如 多伦多', 'aria-label': '所在城市',
      autocomplete: 'off', spellcheck: 'false' });
    var hint = h('p', { class: 'field-help', role: 'status' });
    function drawHint() {
      return api('GET', '/api/location').then(function (w) {
        w = w || {};
        var tz = w.tz ? w.tz + '，UTC' + str(w.offset) : 'UTC' + str(w.offset);
        var guess = w.region ? w.region + (w.city ? '，最可能是' + (/^[\x00-\x7f]/.test(w.city) ? ' ' : '') + w.city : '（具体城市不知道）') : '看不出在哪';
        hint.textContent = (w.setting ? '现在用的是你填的。' : '现在按电脑时区推断：' + guess + '。') + '电脑时区：' + tz + '。';
      }, function () { hint.textContent = ''; });
    }
    function save() {
      var v = String(inp.value || '').replace(/\s+/g, ' ').trim();
      if (v === saved) return;
      return api('POST', '/api/settings', { user_location: v }).then(function () {
        saved = v;
        inp.value = v;
        toast(v ? '记下了：你在' + v + '。' : '已清空，以后按电脑时区推断。', 'info');
        return drawHint();
      });
    }
    inp.addEventListener('change', guard(save));
    inp.addEventListener('keydown', guard(function (e) {
      if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); inp.blur(); }
    }));
    sec.appendChild(h('div', { class: 'field loc-field' }, inp, hint));
    drawHint();
    return sec;
  }

  /** 设置页"操作网页"：开关、允许的网站名单、关闭浏览器。每一项改了立刻保存。 */
  function browserSection(conf) {
    var st = { enabled: conf.enabled !== false, view: conf.view === 'window' ? 'window' : 'panel',
               domains: (Array.isArray(conf.allowed_domains) ? conf.allowed_domains : []).map(str).filter(Boolean) };
    var sec = section('操作网页', 'Agent 打开网页、填表、点按钮，你可以在对话右边的网页面板里看着它做，也可以自己操作。提交、删除、付款之前都会先问你。');
    var off = h('div', { class: 'feature-off', role: 'note' });
    function drawOff() {
      clear(off);
      var g = gate('browser');
      off.hidden = g.ok || !g.reason;
      if (!off.hidden) {
        off.appendChild(h('span', { class: 'feature-off-text', text: g.reason }));
        if (g.fallback) off.appendChild(h('span', { class: 'feature-off-fallback', text: g.fallback }));
      }
    }
    drawOff();
    sec.appendChild(off);

    function save(undo) {
      var b = { enabled: st.enabled, allowed_domains: st.domains.slice() };
      return api('POST', '/api/settings', { browser: b }).then(function () {
        return loadCaps().then(drawOff, drawOff);
      }, function (e) { undo(); throw e; });
    }

    var onBox = h('input', { type: 'checkbox', checked: st.enabled });
    onBox.addEventListener('change', guard(function () {
      var prev = st.enabled;
      st.enabled = onBox.checked;
      return save(function () { st.enabled = prev; onBox.checked = prev; }).then(function () {
        toast(st.enabled ? '已允许 Agent 操作网页。' : '已关掉操作网页。', 'info');
      });
    }));
    sec.appendChild(h('label', { class: 'check' }, onBox, h('span', { text: '允许 Agent 操作网页' })));

    // 网页在哪儿显示（docs/操控电脑.md 第 9 节）：换了要重开浏览器，登录状态留着
    var VIEWS = [{ id: 'panel', label: '程序里的网页面板', desc: '不另开浏览器窗口。在对话右边看，也可以直接在面板里点、打字。' },
                 { id: 'window', label: '单独的浏览器窗口', desc: '像以前那样另开一个看得见的 Chrome 窗口；面板里同步显示。' }];
    var vr = h('div', { class: 'radios', role: 'radiogroup', 'aria-label': '网页显示在哪里' });
    VIEWS.forEach(function (m) {
      var r = h('input', { type: 'radio', name: 'browser_view', value: m.id, checked: st.view === m.id,
        on: { change: guard(function () {
          var prev = st.view;
          st.view = m.id;
          return api('POST', '/api/settings', { browser: { view: m.id } }).then(function () {
            toast(m.id === 'panel' ? '以后网页显示在程序里的面板中。' : '以后另开一个浏览器窗口。', 'info');
          }, function (e) {
            st.view = prev;
            var back = vr.querySelector('input[value="' + prev + '"]');
            if (back) back.checked = true;
            throw e;
          });
        }) } });
      vr.appendChild(h('label', { class: 'radio' }, r, h('span', { class: 'radio-text' },
        h('span', { class: 'radio-label', text: m.label }), h('span', { class: 'radio-desc', text: m.desc }))));
    });
    sec.appendChild(h('div', { class: 'kv-caption', text: '网页显示在' }));
    sec.appendChild(vr);

    sec.appendChild(h('div', { class: 'kv-caption', text: '允许的网站' }));
    sec.appendChild(h('p', { class: 'field-help domain-help', text: '名单里的网站打开时不再问你，子域名也算（填 example.com，a.example.com 也算）。在页面上的提交、删除照样会问。' }));
    var listBox = h('ul', { class: 'domain-list' });
    function drawList() {
      clear(listBox);
      if (!st.domains.length) listBox.appendChild(h('li', { class: 'domain-empty', text: '还没有。打开不在名单里的网站时会先问你，在批准卡片上点「以后都允许这个网站」也会加到这里。' }));
      st.domains.forEach(function (dmn) {
        listBox.appendChild(h('li', { class: 'domain-row' },
          h('span', { class: 'mono domain-name', text: dmn }),
          h('button', { class: 'btn btn-ghost btn-sm btn-danger-text', type: 'button', text: '删除', 'aria-label': '从名单里删除 ' + dmn,
            on: { click: function () {
              var prev = st.domains.slice();
              st.domains = st.domains.filter(function (x) { return x !== dmn; });
              drawList();
              return save(function () { st.domains = prev; drawList(); });
            } } })));
      });
    }
    drawList();
    sec.appendChild(listBox);

    var inp = h('input', { type: 'text', class: 'mono', placeholder: 'oa.example.com', 'aria-label': '要允许的网站',
      autocomplete: 'off', spellcheck: 'false' });
    var err = h('p', { class: 'form-error', role: 'alert', hidden: true });
    function add() {
      var r = addDomain(st.domains, inp.value);
      err.hidden = !r.error;
      err.textContent = r.error;
      inp.setAttribute('aria-invalid', r.error ? 'true' : 'false');
      if (r.error) { inp.focus(); return; }
      var prev = st.domains.slice();
      st.domains = r.list;
      inp.value = '';
      drawList();
      return save(function () { st.domains = prev; drawList(); });
    }
    inp.addEventListener('keydown', guard(function (e) {
      if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); return add(); }
    }));
    inp.addEventListener('input', function () { if (!err.hidden) { err.hidden = true; inp.setAttribute('aria-invalid', 'false'); } });
    sec.appendChild(h('div', { class: 'domain-add' }, inp,
      h('button', { class: 'btn', type: 'button', text: '添加', on: { click: add } })));
    sec.appendChild(err);

    sec.appendChild(h('div', { class: 'form-actions browser-actions' },
      h('button', { class: 'btn btn-ghost', type: 'button', text: '关闭浏览器', title: '关掉 Agent 用的浏览器。登录状态会留着',
        on: { click: function (ev) {
          var b = ev.currentTarget;
          b.disabled = true;
          return api('POST', '/api/browser/close').then(function () {
            b.disabled = false;
            toast('浏览器已关闭。', 'info');
          }, function (e) { b.disabled = false; throw e; });
        } } })));
    return sec;
  }

  /** 设置 → 高级 → 模型能力：每个模型一行；检测到更大的上下文时提示；重新检测；各功能的状态。 */
  function capsSection() {
    var sec = section('模型能力', '接了几个模型，各自能做什么。功能能不能用由这里决定：有一个模型够格就能用。');
    var box = h('div', { class: 'caps-box' });
    var probing = false;
    var btn = h('button', { class: 'btn', type: 'button', text: '重新检测' });
    btn.addEventListener('click', guard(function () {
      if (probing) return;
      probing = true;
      btn.disabled = true;
      btn.textContent = '正在检测…';
      return api('POST', '/api/capabilities/probe').then(function (d) {
        var c = normalizeCaps(d);
        if (c) S.caps = c;
        done();
        toast('检测完了。', 'info');
      }, function (e) { done(); throw e; });
      function done() { probing = false; btn.disabled = false; btn.textContent = '重新检测'; draw(); }
    }));
    function cell(c) {
      return h('td', { class: 'cap-cell cap-' + (c.value === '是' ? 'yes' : c.value === '否' ? 'no' : 'unknown') },
        h('span', { class: 'cap-val', text: c.value }), c.source ? h('span', { class: 'cap-src', text: c.source }) : null);
    }
    function draw() {
      clear(box);
      var caps = S.caps || {};
      var rows = modelRows(caps);
      if (!rows.length) box.appendChild(emptyRow('还没有模型的信息。'));
      else {
        var tbl = h('table', { class: 'table caps-table' }, h('thead', {}, h('tr', {},
          h('th', { text: '模型' }), h('th', { text: '在线' }), h('th', { text: '工具调用' }), h('th', { text: '看图' }),
          h('th', { text: '指位置' }), h('th', { class: 'num', text: '上下文' }))));
        var tb = h('tbody');
        rows.forEach(function (r) {
          tb.appendChild(h('tr', {},
            h('td', {}, h('div', { text: r.name || '模型' }), r.model ? h('div', { class: 'small muted mono', text: r.model }) : null),
            h('td', { class: 'cap-cell cap-' + (r.aliveOk === true ? 'yes' : r.aliveOk === false ? 'down' : 'unknown') },
              h('span', { class: 'cap-val', text: r.alive })),
            cell(r.cells[0]), cell(r.cells[1]), cell(r.cells[2]),
            h('td', { class: 'num' }, h('span', { class: 'cap-val', text: r.context }))));
        });
        tbl.appendChild(tb);
        box.appendChild(h('div', { class: 'table-wrap' }, tbl));
      }
      var hints = rows.filter(function (r) { return r.hint; });
      if (hints.length) {
        box.appendChild(h('p', { class: 'caps-note' }, hints.map(function (r) {
          return (rows.length > 1 ? r.name + '：' : '') + r.hint + '。';
        }).join(' ') + ' 要用更大的上下文，改配置文件里的 context_window。'));
      }
      if (caps.embedding) box.appendChild(h('p', { class: 'caps-note' }, '向量模型：' + embeddingLine(caps.embedding) +
        (caps.embedding.kb_engine ? '。向量库：' + kbEngineText(caps.embedding.kb_engine) : '') + '。'));
      var feats = featureLines(caps);
      if (feats.length) {
        box.appendChild(h('div', { class: 'kv-caption caps-feat-cap', text: '各功能' }));
        box.appendChild(featureList(feats, 'feat-list'));
      }
    }
    draw();
    sec.appendChild(box);
    sec.appendChild(h('div', { class: 'form-actions caps-actions' }, btn));
    return sec;
  }

  /** 设置页"外观"：跟随系统加十个主题。每张卡片用那个主题自己的变量画一个小预览，点一下立刻换。 */
  function themeSection() {
    var T = themeApi();
    var sec = section('外观', '换一套配色。点一下立刻生效，不用保存。');
    if (!T) { sec.appendChild(emptyRow('主题没有装好，先用默认的样子。')); return sec; }
    var items = [{ id: 'auto', name: '跟随系统', sub: '浅色用素白，深色用墨夜' }].concat(T.list.map(function (t) {
      return { id: t.id, name: str(t.name), sub: t.dark ? '深色' : '浅色' };
    }));
    var grid = h('div', { class: 'themes', role: 'radiogroup', 'aria-label': '主题' });
    var cards = [];

    function preview(id) {
      return h('span', { class: 'tp-half', 'data-theme': id },
        h('span', { class: 'tp-side' }, h('i'), h('i'), h('i')),
        h('span', { class: 'tp-main' }, h('span', { class: 'tp-bubble' }), h('span', { class: 'tp-line' }),
          h('span', { class: 'tp-line short' }), h('span', { class: 'tp-btn' })));
    }
    function pick(i, focus) {
      var it = items[i];
      if (!it) return;
      if (T.pref() !== it.id) setTheme(it.id);
      mark();
      if (focus) cards[i].focus();
    }
    function mark() {
      var pref = T.pref();
      cards.forEach(function (c, i) {
        var on = items[i].id === pref;
        c.setAttribute('aria-checked', on ? 'true' : 'false');
        c.tabIndex = on ? 0 : -1;
      });
    }
    items.forEach(function (it, i) {
      var tp = h('span', { class: 'tp', 'aria-hidden': 'true' },
        it.id === 'auto' ? [preview(T.LIGHT || 'plain'), preview(T.DARK || 'ink-night')] : preview(it.id),
        h('span', { class: 'tp-check' }, icon('check', 12)));
      var card = h('button', { class: 'theme-card', type: 'button', role: 'radio', 'aria-checked': 'false', 'data-id': it.id,
        title: it.name + '（' + it.sub + '）',
        on: {
          click: function () { pick(i, false); },
          keydown: function (e) {
            var n = items.length, j = -1;
            if (e.key === 'ArrowRight' || e.key === 'ArrowDown') j = (i + 1) % n;
            else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') j = (i + n - 1) % n;
            else if (e.key === 'Home') j = 0;
            else if (e.key === 'End') j = n - 1;
            if (j < 0) return;
            e.preventDefault();
            pick(j, true);
          }
        } },
        tp, h('span', { class: 'theme-name' }, h('b', { text: it.name }), h('span', { text: it.sub })));
      cards.push(card);
      grid.appendChild(card);
    });
    mark();
    // 用 /主题 指令或者别的页面换了主题，回到这里时选中项跟着变
    T.onChange(function () { if (grid.isConnected) mark(); });
    sec.appendChild(grid);
    return sec;
  }

  var ASR_ENGINES = [
    { id: 'vllm', label: '内网识别服务', desc: '用公司服务器上的语音识别服务，这台电脑不用装任何东西。' },
    { id: 'sherpa', label: '本机识别', desc: '在这台电脑上识别，断网也能用，需要先装好识别程序和模型。' },
    { id: 'off', label: '关闭', desc: '不用语音输入。点麦克风按钮会提示到这里开启。' }
  ];

  /** 设置页的“语音识别”一节。 */
  function asrSection(asr) {
    var f = {
      engine: ['vllm', 'sherpa', 'off'].indexOf(asr.engine) >= 0 ? asr.engine : 'off',
      base_url: str(asr.base_url), model: str(asr.model), api_key: str(asr.api_key),
      language: ['zh', 'en'].indexOf(asr.language) >= 0 ? asr.language : '', sherpa_model_dir: str(asr.sherpa_model_dir)
    };
    var sec = section('语音识别', '输入框旁边的麦克风按钮用它把说的话转成文字。录音只在内存里处理，识别完就丢掉，不保存。');
    var radios = h('div', { class: 'radios', role: 'radiogroup', 'aria-label': '识别方式' });
    ASR_ENGINES.forEach(function (m) {
      var r = h('input', { type: 'radio', name: 'asr_engine', value: m.id, checked: f.engine === m.id,
        on: { change: function () { f.engine = m.id; drawFields(); } } });
      radios.appendChild(h('label', { class: 'radio' }, r, h('span', { class: 'radio-text' },
        h('span', { class: 'radio-label', text: m.label }), h('span', { class: 'radio-desc', text: m.desc }))));
    });
    sec.appendChild(radios);

    function inp(key, ph, cls, type) {
      return h('input', { type: type || 'text', class: cls || '', value: f[key], placeholder: ph, 'aria-label': ph,
        on: { input: function (ev) { f[key] = ev.target.value; } } });
    }
    var lang = h('select', { 'aria-label': '语言' },
      h('option', { value: '', text: '自动判断（中文、英文都行）' }),
      h('option', { value: 'zh', text: '中文' }),
      h('option', { value: 'en', text: '英文' }));
    lang.value = f.language;
    lang.addEventListener('change', function () { f.language = lang.value; });

    var vllmBox = h('div', { class: 'asr-fields' },
      field('服务地址', inp('base_url', 'http://10.0.0.1:8001/v1', 'mono'), '管理员提供的地址，一般以 /v1 结尾。'),
      field('模型', inp('model', 'Qwen/Qwen3-ASR-1.7B', 'mono')),
      field('密钥', inp('api_key', '没有可留空', 'mono', 'password')));
    var sherpaBox = h('div', { class: 'asr-fields' },
      field('模型目录', inp('sherpa_model_dir', '例如 D:\\模型\\sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17', 'mono'),
        '目录里要有 model.int8.onnx（或 model.onnx）和 tokens.txt。'));
    var langBox = h('div', { class: 'asr-fields' }, field('语言', lang, '大多数时候选“自动判断”。只说一种语言时固定下来可能更准。'));
    sec.appendChild(vllmBox);
    sec.appendChild(sherpaBox);
    sec.appendChild(langBox);
    function drawFields() {
      show(vllmBox, f.engine === 'vllm');
      show(sherpaBox, f.engine === 'sherpa');
      show(langBox, f.engine !== 'off');
    }
    drawFields();

    function payload() {
      return { engine: f.engine, base_url: f.base_url.trim(), model: f.model.trim(), api_key: f.api_key,
        language: f.language, sherpa_model_dir: f.sherpa_model_dir.trim() };
    }
    var out = h('div', { class: 'test-out' });
    sec.appendChild(h('div', { class: 'form-actions' },
      h('button', { class: 'btn btn-primary', type: 'button', text: '保存', on: { click: function () {
        var p = payload();
        if (p.engine === 'vllm' && !p.base_url) throw new Error('选了内网识别服务，要填写服务地址。');
        if (p.engine === 'sherpa' && !p.sherpa_model_dir) throw new Error('选了本机识别，要填写模型目录。');
        return api('POST', '/api/settings', { asr: p }).then(function () {
          S.info.asr_engine = p.engine;
          toast(p.engine === 'off' ? '语音输入已关闭。' : '语音识别已保存。可以点“测试”看看通不通。', 'info');
        });
      } } }),
      h('button', { class: 'btn lock-ok', type: 'button', text: '测试', on: { click: function (ev) {
        var b = ev.currentTarget;
        b.disabled = true;
        clear(out);
        out.appendChild(h('p', { class: 'muted small', text: '正在用一段 1 秒的静音测试，最多等一分钟…' }));
        return api('POST', '/api/settings/test_asr', { asr: payload() }).then(function (r) {
          b.disabled = false;
          clear(out);
          r = r || {};
          var name = (ASR_ENGINES.filter(function (m) { return m.id === r.engine; })[0] || {}).label || str(r.engine);
          out.appendChild(h('div', { class: 'test-row ' + (r.ok ? 'is-ok' : 'is-bad') },
            h('span', { text: name }),
            h('span', { class: 'test-state', text: r.ok ? '通了' : '不通' }),
            h('span', { class: 'small muted', text: str(r.detail) })));
        }, function (e) { b.disabled = false; clear(out); throw e; });
      } } })));
    sec.appendChild(out);
    return sec;
  }

  var DECISION_TEXT = { allow: '自动放行', ask: '问了用户', deny: '被禁用', approved: '用户批准', denied: '用户拒绝', auto: '自动放行' };

  function loadAudit(box) {
    clear(box);
    box.appendChild(h('p', { class: 'muted small', text: '正在加载…' }));
    return api('GET', apiUrl('/api/audit', { limit: 100 })).then(function (rows) {
      clear(box);
      rows = asList(rows, 'audit');
      if (!rows.length) { box.appendChild(emptyRow('还没有记录。')); return; }
      var tbl = h('table', { class: 'table table-dense' }, h('thead', {}, h('tr', {},
        h('th', { text: '时间' }), h('th', { text: '动作' }), h('th', { text: '影响' }), h('th', { text: '决定' }), h('th', { text: '结果' }))));
      var tb = h('tbody');
      rows.forEach(function (r) {
        tb.appendChild(h('tr', {},
          h('td', { class: 'small nowrap', text: formatTime(r.ts) }),
          h('td', {}, h('div', { text: toolInfo(r.tool, {}).verb }), h('div', { class: 'mono small muted', text: r.tool })),
          h('td', { class: 'small', text: riskLabel(r.risk) }),
          h('td', { class: 'small', text: DECISION_TEXT[r.decision] || r.decision || '' }),
          h('td', { class: 'small' }, h('div', { text: r.outcome || '' }), r.detail ? h('div', { class: 'muted', text: clip(r.detail, 100) }) : null)));
      });
      tbl.appendChild(tb);
      box.appendChild(h('div', { class: 'table-wrap' }, tbl));
    });
  }

  /* ---------------- 命令面板 ---------------- */

  var palette = { items: [], shown: [], sel: 0, returnFocus: null };

  function paletteItems() {
    var items = [{ label: '新对话', hint: '回到输入框', keywords: 'new xin duihua 新建 任务', run: function () { go('#/new'); } },
      { label: '搜以前的对话', hint: '按说过的话查找', keywords: '搜索 查找 历史 search 任务', run: focusSearch }];
    S.sessions.forEach(function (s) {
      items.push({ label: s.title || '未命名对话', hint: statusText(s.status) + '，' + relativeTime(s.updated), keywords: '对话 任务',
        group: '对话', run: function () { go('#/task/' + encodeURIComponent(s.id)); } });
    });
    Object.keys(PAGES).forEach(function (k) {
      items.push({ label: '打开' + PAGES[k].title, hint: '页面', keywords: k, run: function () { go('#/' + k); } });
    });
    if (S.defaultWs != null && S.wsId !== S.defaultWs) {
      items.push({ label: '回到全部对话', hint: '不用文件夹', keywords: '默认 文件夹 切换', run: function () { setWorkspace(S.defaultWs); } });
    }
    folderList(S.workspaces, S.defaultWs, S.wsId).forEach(function (w) {
      if (w.current) return;
      items.push({ label: '切换到「' + w.name + '」文件夹', hint: w.path, keywords: '文件夹 工作区 切换', run: function () { setWorkspace(w.id); } });
    });
    items.push({ label: '添加文件夹', hint: '用一个文件夹工作', keywords: '文件夹 工作区 连接', run: function () { return connectFolder(); } });
    var vkey = (isMac() ? '⌘' : 'Ctrl') + ' + Shift + M';
    if (V.state === 'recording') items.unshift({ label: '结束录音并识别', hint: vkey, keywords: '语音 麦克风 voice yuyin', run: function () { return toggleVoice(); } });
    else if (V.state === 'idle' && voiceTarget()) items.push({ label: '语音输入', hint: vkey, keywords: '语音 说话 麦克风 录音 voice yuyin', run: function () { return toggleVoice(); } });
    MODES.forEach(function (m) {
      items.push({ label: '批准方式改为「' + m.label + '」', hint: m.desc, keywords: '批准 模式', run: function () { return setMode(m.id); } });
    });
    return items;
  }

  function openPalette() {
    var p = $('palette'), input = $('palette-input');
    if (!p || !input) return;
    closeMenus();
    palette.returnFocus = document.activeElement;
    palette.items = paletteItems();
    input.value = '';
    palette.sel = 0;
    drawPalette();
    p.hidden = false;
    syncScrim();
    document.body.classList.add('palette-open');
    input.focus();
  }

  function closePalette() {
    var p = $('palette');
    if (!p || p.hidden) return false;
    p.hidden = true;
    document.body.classList.remove('palette-open');
    syncScrim();
    if (palette.returnFocus && palette.returnFocus.focus) { try { palette.returnFocus.focus(); } catch (e) { /* 忽略 */ } }
    return true;
  }

  function drawPalette() {
    var list = $('palette-list'), input = $('palette-input');
    if (!list || !input) return;
    palette.shown = filterCommands(palette.items, input.value).slice(0, 40);
    if (palette.sel >= palette.shown.length) palette.sel = Math.max(0, palette.shown.length - 1);
    clear(list);
    if (!palette.shown.length) { list.appendChild(h('p', { class: 'list-empty', text: '没有找到。' })); return; }
    palette.shown.forEach(function (it, i) {
      var sel = i === palette.sel;
      list.appendChild(h('div', { class: 'pal-item' + (sel ? ' is-sel' : ''), role: 'option', id: 'pal-' + i,
        'aria-selected': sel ? 'true' : 'false',
        on: { mousedown: function (e) { e.preventDefault(); }, click: function () { return runPalette(i); },
              mousemove: function () { if (palette.sel !== i) { palette.sel = i; drawPalette(); } } } },
        h('span', { class: 'pal-label', text: it.label }),
        it.hint ? h('span', { class: 'pal-hint', text: it.hint }) : null));
    });
    input.setAttribute('aria-activedescendant', 'pal-' + palette.sel);
    var cur = $('pal-' + palette.sel);
    if (cur && cur.scrollIntoView) cur.scrollIntoView({ block: 'nearest' });
  }

  function runPalette(i) {
    var it = palette.shown[i];
    if (!it) return;
    palette.returnFocus = null;
    closePalette();
    return it.run();
  }

  /* ---------------- 窄屏抽屉 ---------------- */

  function toggleDrawer(side) {
    var cls = 'show-' + side, other = side === 'left' ? 'show-right' : 'show-left';
    var on = !document.body.classList.contains(cls);
    document.body.classList.remove(other);
    document.body.classList.toggle(cls, on);
    syncScrim();
    applyRight();
    if (on) {
      var pane = $(side === 'left' ? 'sidebar' : 'rightpane');
      var f = pane && pane.querySelector('button, a[href], input, select, textarea');
      if (f) f.focus();
    }
  }

  function closeDrawers() {
    var had = document.body.classList.contains('show-left') || document.body.classList.contains('show-right');
    document.body.classList.remove('show-left', 'show-right');
    syncScrim();
    if (had) applyRight();
    return had;
  }

  /* ---------------- 绑定 ---------------- */

  function on(id, ev, fn) {
    var el = $(id);
    if (el) el.addEventListener(ev, guard(fn));
  }

  /** 侧栏收成一条图标时，按钮上的说明跟着换。 */
  function syncCollapse() {
    var b = $('side-collapse');
    if (!b) return;
    var mini = document.body.classList.contains('side-mini');
    b.setAttribute('aria-label', mini ? '展开侧栏' : '收起侧栏');
    b.title = mini ? '展开侧栏' : '收起侧栏';
    b.setAttribute('aria-expanded', mini ? 'false' : 'true');
  }

  function bindStatic() {
    var mac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || '');
    setText($('palette-kbd'), mac ? '⌘ K' : 'Ctrl K');
    paintIcons(document);
    try { document.body.classList.toggle('side-mini', localStorage.getItem('ca.side') === 'mini'); } catch (e) { /* 忽略 */ }
    syncCollapse();
    on('side-collapse', 'click', function () {
      var mini = !document.body.classList.contains('side-mini');
      document.body.classList.toggle('side-mini', mini);
      try { localStorage.setItem('ca.side', mini ? 'mini' : ''); } catch (e) { /* 忽略 */ }
      syncCollapse();
    });

    on('new-task', 'click', function () { go('#/new'); });
    on('open-palette', 'click', openPalette);
    var navs = document.querySelectorAll('.nav-btn[data-view]');
    for (var i = 0; i < navs.length; i++) {
      (function (b) { b.addEventListener('click', guard(function () { go('#/' + b.getAttribute('data-view')); })); })(navs[i]);
    }
    on('task-list', 'click', function () { closeDrawers(); });
    on('list-all', 'click', function () { if (S.defaultWs != null) setWorkspace(S.defaultWs); });

    on('onboard-form', 'submit', submitOnboard);
    on('folder-form', 'submit', submitFolderForm);
    on('folder-cancel', 'click', function () { closeFolderDialog(); });
    on('folder-dialog', 'keydown', function (e) {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeFolderDialog(); return; }
      trapTab(e, $('folder-dialog'));
    });

    bindComposer('home');
    bindComposer('composer');
    on('file-input', 'change', function (e) {
      var which = e.target.getAttribute('data-for') || 'composer';
      if (e.target.files && e.target.files.length) addImages(which, e.target.files);
      e.target.value = '';
    });
    on('file-any', 'change', function (e) {
      var which = e.target.getAttribute('data-for') || 'composer';
      if (e.target.files && e.target.files.length) addFiles(which, Array.prototype.slice.call(e.target.files));
      e.target.value = '';
    });
    on('home-mic', 'click', function () { return toggleVoice('home'); });
    on('composer-mic', 'click', function () { return toggleVoice('composer'); });
    renderVoice();
    on('home-mode', 'click', function (e) { openModeMenu(e.currentTarget); });
    ['home', 'composer'].forEach(function (which) {
      on(which + '-deep', 'click', function () { S.deep[which] = !S.deep[which]; renderDeep(); });
    });
    on('composer-mode', 'click', function (e) { openModeMenu(e.currentTarget); });

    on('btn-more', 'click', function (e) { openTaskMenu(e.currentTarget); });
    on('task-search', 'input', onSearchInput);
    on('task-search', 'keydown', function (e) {
      if (e.key === 'Escape' && e.target.value) {
        // 先清空搜索，不让 Esc 再去关抽屉
        e.preventDefault();
        e.stopPropagation();
        resetSearch(true);
        renderTaskList();
      } else if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) {
        e.preventDefault();
        var st = S.search;
        if (st.timer) { clearTimeout(st.timer); st.timer = null; runSearch(); return; }
        var first = $('task-list') && $('task-list').querySelector('a.task-item');
        if (first) first.click();
      } else if (e.key === 'ArrowDown') {
        var link = $('task-list') && $('task-list').querySelector('a.task-item');
        if (link) { e.preventDefault(); link.focus(); }
      }
    });
    on('btn-right-toggle', 'click', function () { toggleRight(); });
    bindWeb();
    on('btn-left-toggle', 'click', function () { toggleDrawer('left'); });
    on('jump-attention', 'click', jumpToAttention);
    on('scrim', 'click', function () { closePalette(); closeFolderDialog(); closeDrawers(); });
    var wrap = $('timeline-wrap');
    if (wrap) wrap.addEventListener('scroll', function () { updateJump(); }, { passive: true });

    on('palette-input', 'input', function () { palette.sel = 0; drawPalette(); });
    on('palette-input', 'keydown', function (e) {
      if (e.key === 'ArrowDown') { e.preventDefault(); palette.sel = Math.min(palette.sel + 1, palette.shown.length - 1); drawPalette(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); palette.sel = Math.max(palette.sel - 1, 0); drawPalette(); }
      else if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); return runPalette(palette.sel); }
      else if (e.key === 'Tab') { e.preventDefault(); }
    });

    document.addEventListener('keydown', guard(function (e) {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault();
        if (!closePalette()) openPalette();
        return;
      }
      if (isVoiceShortcut(e)) {
        e.preventDefault();
        closePalette();
        return toggleVoice();
      }
      if (e.key === 'Escape') {
        if (closePalette()) return;
        if (closeSlashCard(true)) { e.preventDefault(); return; }
        if (closeFolderDialog()) { e.preventDefault(); return; }
        if (closeDocCard()) { e.preventDefault(); return; }
        if (!e.isComposing && (V.state === 'recording' || V.state === 'starting' || V.state === 'transcribing')) { e.preventDefault(); cancelVoice(); return; }
        if (closeMenus(true)) { e.preventDefault(); return; }
        if (closeDrawers()) return;
        if (S.preview && S.route === 'task') { closePreview(); return; }
      }
    }));
    // 指令菜单、指令卡片：点外面就关上（指令菜单在输入框失去焦点时也会关）
    document.addEventListener('mousedown', function (e) {
      var t = e.target;
      var card = $('slash-card'), menu = $('slash-menu');
      if (card && !card.hidden && !card.contains(t)) closeSlashCard(false);
      if (SL.which && menu && !menu.contains(t) && t !== slashInput(SL.which)) closeSlash();
      var hm = $('hash-menu');
      if (HP.which && hm && !hm.contains(t) && t !== composerFor(HP.which).input) closeHash();
    }, true);
    // 点菜单外面就关上菜单（点打开它的按钮由按钮自己处理：再点一次是关）
    document.addEventListener('click', function (e) {
      var t = e.target;
      var openMenu = MENUS.some(function (id) { var m = $(id); return m && !m.hidden; });
      if (!openMenu) return;
      var inside = MENUS.some(function (id) { var m = $(id); return m && !m.hidden && m.contains(t); });
      if (inside || (pop.btn && pop.btn.contains(t))) return;
      closeMenus(false);
    });
    // 菜单里用上下键移动，Tab 离开时关上
    MENUS.forEach(function (id) {
      on(id, 'keydown', function (e) {
        if (e.key === 'Tab') { closeMenus(false); return; }
        if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp' && e.key !== 'Home' && e.key !== 'End') return;
        e.preventDefault();
        var items = Array.prototype.slice.call($(id).querySelectorAll('.menu-item:not([disabled])'));
        if (!items.length) return;
        var i = items.indexOf(document.activeElement);
        var n;
        if (e.key === 'Home' || (i < 0 && e.key === 'ArrowDown')) n = items[0];
        else if (e.key === 'End' || (i < 0 && e.key === 'ArrowUp')) n = items[items.length - 1];
        else n = items[(i + (e.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length];
        if (n) n.focus();
      });
    });
    bindDrop();
    window.addEventListener('hashchange', guard(function () {
      if (V.state === 'recording' || V.state === 'starting') { cancelVoice(true); toast('离开了页面，录音已取消。', 'info'); }
      closeSlash();
      closeSlashCard(false);
      route();
    }));
    window.addEventListener('pagehide', function () { if (V.state !== 'idle') cancelVoice(true); });
    window.addEventListener('resize', function () {
      updateJump(); applyRight(); closeMenus(false);
      if (SL.which) placeSlash($('slash-menu'), SL.which);
      if (SL.card) placeSlash($('slash-card'), SL.card);
    });
    window.addEventListener('error', function (e) {
      if (Date.now() - S.lastRenderError > 5000) { S.lastRenderError = Date.now(); toast('界面出了点问题：' + ((e && e.message) || '未知错误') + '。刷新页面通常能解决。'); }
    });
  }

  /* ---------------- 把文件拖到窗口上 ----------------
   * dragenter / dragleave 在子元素之间会反复触发，用计数判断是不是真的离开了窗口；只在拖的是文件时提示。
   */

  var drag = { depth: 0 };

  function dropTarget() {
    var d = $('folder-dialog');
    if (d && !d.hidden) return null;
    return voiceTarget();      // 新对话页 → home，对话页 → composer，其他页面不收
  }

  function setDropShown(on) {
    var o = $('drop-overlay');
    if (o) o.hidden = !on;
  }

  function bindDrop() {
    window.addEventListener('dragenter', function (e) {
      if (!isFileDrag(e.dataTransfer)) return;
      e.preventDefault();
      drag.depth++;
      if (dropTarget()) setDropShown(true);
    });
    window.addEventListener('dragover', function (e) {
      if (!isFileDrag(e.dataTransfer)) return;
      e.preventDefault();      // 不拦的话浏览器会直接打开这个文件，离开当前页面
      try { e.dataTransfer.dropEffect = dropTarget() ? 'copy' : 'none'; } catch (x) { /* 忽略 */ }
    });
    window.addEventListener('dragleave', function (e) {
      if (!isFileDrag(e.dataTransfer)) return;
      drag.depth = Math.max(0, drag.depth - 1);
      // 有的浏览器计数会对不上：鼠标已经在窗口外面时直接归零
      var w = window.innerWidth || 0, hh = window.innerHeight || 0;
      var outside = !e.relatedTarget && (e.clientX <= 0 || e.clientY <= 0 || (w && e.clientX >= w) || (hh && e.clientY >= hh));
      if (drag.depth === 0 || outside) { drag.depth = 0; setDropShown(false); }
    });
    window.addEventListener('drop', guard(function (e) {
      if (!isFileDrag(e.dataTransfer)) return;
      e.preventDefault();
      drag.depth = 0;
      setDropShown(false);
      var which = dropTarget();
      var files = e.dataTransfer && e.dataTransfer.files ? Array.prototype.slice.call(e.dataTransfer.files) : [];
      if (!files.length) return;
      if (!which) { toast('回到对话页再把文件拖进来。', 'info'); return; }
      addFiles(which, files);
      var c = composerFor(which);
      if (c.input) c.input.focus();
    }));
    window.addEventListener('dragend', function () { drag.depth = 0; setDropShown(false); });
  }

  function pickImages(which) {
    var f = $('file-input');
    if (!f) return;
    f.setAttribute('data-for', which);
    f.click();
  }

  // 每分钟刷新一次列表里的“几分钟前”
  setInterval(function () { if (S.token && S.wsId) markDirty('list'); }, 60000);

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', guard(boot));
  else guard(boot)();
})();
