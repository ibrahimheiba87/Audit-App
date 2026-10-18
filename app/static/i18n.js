// English/Arabic switch. The page and app.js stay in English; in Arabic, every piece of
// text that reaches the page (including the server's labels and error messages) is looked
// up in the dictionary in ar.js and swapped for its translation as it appears. The English
// is kept beside each translation, so switching back restores it without a reload.
//
// Uploaded data is never translated: mark anything showing it with translate="no".
// Numbers and dates keep Western digits so figures match the source files.

const I18N = (() => {
  const KEY = "audit-lang";
  const ATTRS = ["placeholder", "title", "aria-label", "label", "alt"];
  const SKIP = new Set(["SCRIPT", "STYLE", "TEXTAREA", "CODE", "PRE"]);
  const norm = (s) => s.replace(/\s+/g, " ").trim();

  let lang = "en";
  try { if (localStorage.getItem(KEY) === "ar") lang = "ar"; } catch {}

  // Dictionary, filled from ar.js: exact strings, and patterns with {name} placeholders.
  // A {t:name} placeholder's value is translated too (a role, status or label).
  const exact = new Map();
  const patterns = [];

  function add(dict) {
    for (const [en, ar] of Object.entries(dict.exact || {})) exact.set(norm(en), ar);
    for (const [en, ar] of Object.entries(dict.patterns || {})) {
      const names = [], translate = [];
      let re = "", literal = "", size = 0;
      for (const part of norm(en).split(/(\{(?:t:)?\w+\})/)) {
        const m = part.match(/^\{(t:)?(\w+)\}$/);
        if (m) { names.push(m[2]); translate.push(!!m[1]); re += "(.+?)"; }
        else {
          re += part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
          size += part.length;
          if (part.length > literal.length) literal = part;
        }
      }
      patterns.push({ re: new RegExp(`^${re}$`, "s"), names, translate, ar, literal: literal.trim(), size });
    }
    // Most specific first, so "Added {name} as {t:role}" doesn't beat "… as {t:role} of {client}".
    patterns.sort((a, b) => b.size - a.size);
  }

  // A run of text kept in its own direction inside Arabic: next to Arabic letters, a date
  // like 2026-12-31 would otherwise show as 31-12-2026. The marks are invisible but are
  // copied with the text, so bare figures only get them when a minus or hyphen needs it.
  const isolate = (v) => `\u2068${v}\u2069`;
  const capitalised = (v) => v.charAt(0).toUpperCase() + v.slice(1);

  // Dates and times app.js has shown, by their English text (see when() there), so they
  // can be shown the Arabic way: Arabic order and ص/م for AM/PM, still with Western digits.
  const dates = new Map();
  const AR_DATES = (() => {
    try { return new Intl.Locale("ar", { numberingSystem: "latn", calendar: "gregory" }).toString(); }
    catch { return "ar"; }
  })();
  function when(date, english) {
    dates.set(norm(english), date);
    return english;
  }
  const arDate = (s) => {
    const d = dates.get(s);
    return d ? isolate(d.toLocaleString(AR_DATES)) : null;
  };

  // Translate one piece of text; null when there's nothing for it.
  function lookup(text) {
    const s = norm(text);
    const date = arDate(s);
    if (date) return date;
    const plain = /\d/.test(s) && !/[\u0600-\u06ff\u2068]/.test(s);  // figures, no Arabic yet
    if (!/[A-Za-z]/.test(s)) return plain && /-\s*\d/.test(s) ? s.replace(/-?\d(.*\d)?/s, isolate) : null;
    const hit = exact.get(s);
    if (hit !== undefined) return hit;
    for (const p of patterns) {
      if (p.literal && !s.includes(p.literal)) continue;
      const m = s.match(p.re);
      if (!m) continue;
      const vals = {};
      // Values (names, files, dates, amounts) are isolated, so their own direction holds.
      // A label can arrive lower-cased ("Total cash and cash equivalents").
      p.names.forEach((n, i) => {
        const v = m[i + 1];
        vals[n] = arDate(v) || (p.translate[i] && (lookup(v) ?? exact.get(capitalised(v)))) || isolate(v);
      });
      return p.ar.replace(/\{(\w+)\}/g, (_, n) => vals[n] ?? "");
    }
    return plain && /^\d/.test(s) ? isolate(s) : null;  // such as a date and time: 10/13/2026, 9:05 AM
  }

  // Text for the current language, keeping the spaces around it.
  function tr(text) {
    if (lang !== "ar" || text == null) return text;
    const str = String(text);
    const out = lookup(str);
    if (out == null) return str;
    const [, lead, , tail] = str.match(/^(\s*)([\s\S]*?)(\s*)$/);
    return lead + out + tail;
  }

  // English behind each translated text node and attribute: node -> {src, out, lang}.
  const texts = new WeakMap();
  const attrs = new WeakMap();

  function source(state, current) {
    return state && current === state.out ? state.src : current;
  }

  function doText(node) {
    const state = texts.get(node);
    if (state?.lang === lang && node.nodeValue === state.out) return;  // our own change
    const src = source(state, node.nodeValue);
    const out = tr(src);
    if (out === src && !state) return;
    const parent = node.parentElement;
    // An <option> without a value submits its text, so pin the English before translating.
    if (parent?.tagName === "OPTION" && !parent.hasAttribute("value")) parent.setAttribute("value", norm(src));
    texts.set(node, { src, out, lang });
    if (node.nodeValue !== out) node.nodeValue = out;
  }

  function doAttr(el, name) {
    if (!el.hasAttribute(name)) return;
    let map = attrs.get(el);
    const state = map?.[name];
    if (state?.lang === lang && el.getAttribute(name) === state.out) return;
    const src = source(state, el.getAttribute(name));
    const out = tr(src);
    if (out === src && !state) return;
    if (!map) attrs.set(el, map = {});
    map[name] = { src, out, lang };
    if (el.getAttribute(name) !== out) el.setAttribute(name, out);
  }

  const excluded = (el) => !!el?.closest?.('[translate="no"]');

  function walk(node) {
    if (node.nodeType === Node.TEXT_NODE) {
      const parent = node.parentElement;
      if (!excluded(parent) && !SKIP.has(parent?.tagName)) doText(node);
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE || excluded(node)) return;
    const w = document.createTreeWalker(node, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
      acceptNode: (n) => n.nodeType === Node.ELEMENT_NODE
        ? (n.getAttribute("translate") === "no" ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT)
        : (SKIP.has(n.parentElement?.tagName) ? NodeFilter.FILTER_SKIP : NodeFilter.FILTER_ACCEPT),
    });
    for (let n = w.currentNode; n; n = w.nextNode()) {
      if (n.nodeType === Node.TEXT_NODE) doText(n);
      else {
        for (const a of ATTRS) doAttr(n, a);
        if (n.tagName === "INPUT" && (n.type === "button" || n.type === "submit")) doAttr(n, "value");
      }
    }
  }

  const observer = new MutationObserver((records) => {
    for (const r of records) {
      if (r.type === "childList") r.addedNodes.forEach(walk);
      else if (r.type === "characterData") {
        const parent = r.target.parentElement;
        if (!excluded(parent) && !SKIP.has(parent?.tagName)) doText(r.target);
      }
      else if (!excluded(r.target)) doAttr(r.target, r.attributeName);
    }
  });

  const TITLE = document.title;

  function apply() {
    const root = document.documentElement;
    root.lang = lang;
    root.dir = lang === "ar" ? "rtl" : "ltr";
    document.title = tr(TITLE);
    if (document.body) walk(document.body);
    const btn = document.getElementById("lang-toggle");
    if (btn) {
      // Named in the language it switches to, so it's readable whichever is showing.
      btn.textContent = lang === "ar" ? "English" : "العربية";
      btn.lang = lang === "ar" ? "en" : "ar";
      btn.setAttribute("aria-label", lang === "ar" ? "Switch to English" : "التبديل إلى العربية");
    }
  }

  function set(next, { remember = true } = {}) {
    next = next === "ar" ? "ar" : "en";
    if (remember) { try { localStorage.setItem(KEY, next); } catch {} }
    if (next === lang) return;
    lang = next;
    apply();
    document.dispatchEvent(new CustomEvent("languagechange", { detail: lang }));
  }

  // Dialogs the browser draws itself.
  for (const name of ["alert", "confirm", "prompt"]) {
    const native = window[name].bind(window);
    window[name] = (msg, ...rest) => native(tr(msg), ...rest);
  }

  document.documentElement.lang = lang;
  document.documentElement.dir = lang === "ar" ? "rtl" : "ltr";
  // After app.js has run, so anything it reads from the page at start-up is still English.
  document.addEventListener("DOMContentLoaded", () => {
    apply();
    observer.observe(document.body, { childList: true, characterData: true, subtree: true,
      attributes: true, attributeFilter: ATTRS });
  });

  // Whether the dictionary has `text` itself (not just a pattern it happens to fit).
  const known = (text) => exact.has(norm(text));

  return { add, tr, known, set, when, get lang() { return lang; } };
})();
