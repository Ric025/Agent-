/* 主题：在页面最前面用普通 <script src="/theme.js"> 加载，先把 <html data-theme> 设好，避免闪一下默认主题。
 * 接口：window.CATheme = { list, pref(), resolved(), set(id), onChange(fn) }
 * Node 里 require / import 这个文件只拿到 { list }，不碰 DOM。 */
(function () {
  'use strict';
  var LIST = [
    { id: 'plain', name: '素白', dark: false },
    { id: 'gongwen', name: '公文', dark: false },
    { id: 'celadon', name: '青瓷', dark: false },
    { id: 'ink-night', name: '墨夜', dark: true },
    { id: 'solarized-light', name: 'Solarized Light', dark: false },
    { id: 'catppuccin-latte', name: 'Catppuccin Latte', dark: false },
    { id: 'nord', name: 'Nord', dark: true },
    { id: 'tokyo-night', name: 'Tokyo Night', dark: true },
    { id: 'catppuccin-mocha', name: 'Catppuccin Mocha', dark: true },
    { id: 'gruvbox-dark', name: 'Gruvbox Dark', dark: true }
  ];
  var KEY = 'ca.theme', LIGHT = 'plain', DARK = 'ink-night';

  if (typeof window === 'undefined' || typeof document === 'undefined') {
    if (typeof module === 'object' && module.exports) module.exports = { list: LIST, KEY: KEY, LIGHT: LIGHT, DARK: DARK };
    return;
  }

  function known(id) {
    for (var i = 0; i < LIST.length; i++) if (LIST[i].id === id) return true;
    return false;
  }
  function norm(id) { return known(id) ? id : 'auto'; }

  var mq = null;
  try { mq = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null; } catch (e) { mq = null; }
  function sysDark() { return !!(mq && mq.matches); }

  var pref = 'auto';
  try { pref = norm(window.localStorage.getItem(KEY)); } catch (e) { pref = 'auto'; }

  var current = null, currentPref = null, fns = [];
  function resolve() { return pref === 'auto' ? (sysDark() ? DARK : LIGHT) : pref; }

  function apply() {
    var id = resolve(), root = document.documentElement;
    root.setAttribute('data-theme', id);
    root.setAttribute('data-theme-pref', pref);
    if (id === current && pref === currentPref) return;
    var first = current === null;
    current = id; currentPref = pref;
    if (first) return;
    for (var i = 0; i < fns.length; i++) {
      try { fns[i](id, pref); } catch (e) { if (window.console) console.error(e); }
    }
  }

  function onSys() { if (pref === 'auto') apply(); }
  if (mq) {
    if (mq.addEventListener) mq.addEventListener('change', onSys);
    else if (mq.addListener) mq.addListener(onSys);
  }

  window.CATheme = {
    list: LIST,
    pref: function () { return pref; },
    resolved: function () { return resolve(); },
    // 换主题或换回跟随系统都会通知 onChange（生效主题没变、只是 pref 变了也通知，方便设置页更新选中项）
    set: function (id) {
      pref = norm(id);
      try {
        if (pref === 'auto') window.localStorage.removeItem(KEY);
        else window.localStorage.setItem(KEY, pref);
      } catch (e) { /* 存不了就只在这次生效 */ }
      apply();
    },
    onChange: function (fn) { if (typeof fn === 'function') fns.push(fn); }
  };
  apply();
})();
