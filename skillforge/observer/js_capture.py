"""注入页面的采集脚本。

设计要点（都是踩过的坑）：
  - 事件发生时**只取 before_text**，after_text 由宿主在 settle 之后补 —— 否则断言会基于
    操作前的状态生成（H2 快照时机）。
  - 只存文本摘要 + 语义指纹，不存全量 DOM —— 40 个动作 × 全 DOM 会轻松上百 MB（H4 体积）。
  - 必须注入到**所有 frame**：业务系统大量使用 iframe，只注入顶层等于什么都录不到（H3）。
  - 指纹在事件发生的**那一刻**从 event.target 取，避免重渲染后抓到别的元素。
"""
from __future__ import annotations

import json

_VERSION_PLACEHOLDER = "__VERSION__"

_RAW_JS = r"""
(() => {
  if (window.__skillforgeInstalled) { return; }
  window.__skillforgeInstalled = true;

  const MAX_TEXT = 400;

  function norm(s) {
    if (s === null || s === undefined) { return ''; }
    let t = String(s).replace(/\s+/g, ' ').trim();
    t = t.replace(/[:：*＊\s]+$/, '');
    return t.slice(0, MAX_TEXT);
  }

  function attr(el, name) {
    try { return (el && el.getAttribute) ? el.getAttribute(name) : null; } catch (e) { return null; }
  }

  function inferRole(el) {
    const r = attr(el, 'role');
    if (r) { return r; }
    const tag = (el.tagName || '').toLowerCase();
    if (tag === 'button') { return 'button'; }
    if (tag === 'a') { return 'link'; }
    if (tag === 'select') { return 'combobox'; }
    if (tag === 'textarea') { return 'textbox'; }
    if (tag === 'input') {
      const t = (attr(el, 'type') || 'text').toLowerCase();
      if (t === 'checkbox') { return 'checkbox'; }
      if (t === 'radio') { return 'radio'; }
      if (t === 'submit' || t === 'button') { return 'button'; }
      return 'textbox';
    }
    return tag || 'unknown';
  }

  function accessibleName(el) {
    const aria = attr(el, 'aria-label');
    if (aria) { return norm(aria); }
    const labelledby = attr(el, 'aria-labelledby');
    if (labelledby && document.getElementById) {
      const ref = document.getElementById(labelledby);
      if (ref) { return norm(ref.innerText); }
    }
    const id = attr(el, 'id');
    if (id && document.querySelector) {
      const lab = document.querySelector('label[for="' + String(id).replace(/"/g, '\\"') + '"]');
      if (lab) { return norm(lab.innerText); }
    }
    const wrap = el.closest ? el.closest('label') : null;
    if (wrap) { return norm(wrap.innerText); }
    return norm(attr(el, 'placeholder') || attr(el, 'title') || el.innerText || el.value || '');
  }

  function neighborText(el) {
    let cur = el;
    for (let hop = 0; hop < 3; hop++) {
      if (!cur || !cur.parentElement) { break; }
      cur = cur.parentElement;
      const own = norm(cur.innerText);
      if (own && own.length <= 120) { return own; }
    }
    return '';
  }

  function bboxRel(el) {
    const r = el.getBoundingClientRect();
    const w = window.innerWidth || 1;
    const h = window.innerHeight || 1;
    return [
      Math.round((r.left / w) * 10000) / 10000,
      Math.round((r.top / h) * 10000) / 10000,
      Math.round((r.width / w) * 10000) / 10000,
      Math.round((r.height / h) * 10000) / 10000
    ];
  }

  function pickAttrs(el) {
    const keep = ['type', 'name', 'data-testid', 'aria-label', 'placeholder', 'for'];
    const out = {};
    for (const k of keep) {
      const v = attr(el, k);
      if (v && String(v).length < 80) { out[k] = String(v); }
    }
    return out;
  }

  function fingerprint(el) {
    return {
      role: inferRole(el),
      accessible_name: accessibleName(el),
      text_norm: norm(el.innerText || el.value || ''),
      attrs_subset: pickAttrs(el),
      neighbor_text: neighborText(el),
      bbox_rel: bboxRel(el),
      frame_url: location.href,
      frame_path: window.__skillforgeFramePath || '',
      // 注意：这个占位符故意不加引号。capture_script 用 json.dumps 替换时会自带引号，
      // 若在模板里再套一层引号就会变成双引号嵌套 —— 语法错误，而且只有真浏览器能发现。
      captured_version: __VERSION__
    };
  }

  function formSnapshot() {
    // H4：日期选择器 / 自动完成 / 组件库会用 JS 直接写 value，不触发 change。
    // 这些字段只能靠「提交前扫一遍表单」补录，不能靠事件。
    const out = [];
    const nodes = document.querySelectorAll('input, select, textarea');
    for (const el of nodes) {
      const type = (attr(el, 'type') || '').toLowerCase();
      if (type === 'hidden' || type === 'password' || type === 'submit' || type === 'button') {
        continue;
      }
      if (!el.getClientRects().length) { continue; }
      const checked = (type === 'checkbox' || type === 'radio') ? String(!!el.checked) : '';
      out.push({
        role: inferRole(el),
        accessible_name: accessibleName(el),
        name: attr(el, 'name') || '',
        value: (type === 'checkbox' || type === 'radio') ? checked : String(el.value || ''),
        frame_url: location.href,
        frame_path: window.__skillforgeFramePath || ''
      });
    }
    return out;
  }

  function textDigest() {
    try { return norm(document.body ? document.body.innerText : ''); }
    catch (e) { return ''; }
  }

  function emit(payload) {
    payload.before_text = textDigest();
    payload.after_text = null;   // 宿主 settle 后补
    payload.frame_path = window.__skillforgeFramePath || '';
    try { if (window.__skillforgeLog) { window.__skillforgeLog(payload); } }
    catch (e) { /* 绑定不可用时静默 */ }
  }

  // 值变更采集：不能只监听 change。
  // 实测：page.fill() 只派发 input；用户用自动填充/组件库写值时也可能不 blur。
  // 策略 = input 立刻发（首次）+ 300ms 内节流，change 强制补发，同值去重。
  const lastValue = new WeakMap();
  const lastAt = new WeakMap();
  const timers = new WeakMap();

  function valueOf(el) {
    const type = (el.type || '').toLowerCase();
    if (type === 'checkbox' || type === 'radio') { return String(!!el.checked); }
    return String(el.value === undefined || el.value === null ? '' : el.value);
  }

  function kindOf(el) {
    return (el.tagName || '').toLowerCase() === 'select' ? 'select' : 'fill';
  }

  function emitValue(el) {
    if (!el || el.nodeType !== 1) { return; }
    const v = valueOf(el);
    if (lastValue.get(el) === v) { return; }
    lastValue.set(el, v);
    lastAt.set(el, performance.now());
    emit({ event: kindOf(el), t_ms: Math.round(performance.now()),
           value: v, fingerprint: fingerprint(el) });
  }

  window.addEventListener('input', (e) => {
    const el = e.target;
    if (!el || el.nodeType !== 1) { return; }
    const now = performance.now();
    const last = lastAt.get(el);
    if (last === undefined || now - last > 300) {
      emitValue(el);
    } else {
      clearTimeout(timers.get(el));
      timers.set(el, setTimeout(() => emitValue(el), 200));
    }
  }, true);

  window.addEventListener('change', (e) => {
    const el = e.target;
    if (!el || el.nodeType !== 1) { return; }
    clearTimeout(timers.get(el));
    emitValue(el);
  }, true);

  window.addEventListener('click', (e) => {
    const el = e.target;
    if (!el || el.nodeType !== 1) { return; }
    emit({ event: 'click', t_ms: Math.round(performance.now()),
           value: null, fingerprint: fingerprint(el) });
  }, true);

  // 宿主用：判断文本是否已经稳定（settle）
  window.__skillforgeTextDigest = textDigest;

  // 宿主用：全表单快照（H4：补录不触发 change 的字段）
  window.__skillforgeFormSnapshot = formSnapshot;

  // 宿主用：locator 失效后按指纹收集候选元素
  window.__skillforgeCollectCandidates = function (limit) {
    const nodes = document.querySelectorAll('button, a, input, select, textarea, [role]');
    const cap = limit || 200;
    const out = [];
    for (let i = 0; i < nodes.length && out.length < cap; i++) {
      const el = nodes[i];
      if (!el.getClientRects().length) { continue; }
      out.push(fingerprint(el));
    }
    return out;
  };
})();
"""


def capture_script(version: str = "0.3.0") -> str:
    """返回注入用的 JS，并把版本号烧进指纹（便于追溯是哪版采集器录的）。"""
    return _RAW_JS.replace(_VERSION_PLACEHOLDER, json.dumps(version))


__all__ = ["capture_script"]
