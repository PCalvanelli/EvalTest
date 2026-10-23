// Small shared helpers: DOM lookup, escaping, formatting and the few recurring bits of markup.

export const $ = (s, root = document) => root.querySelector(s);
export const $$ = (s, root = document) => [...root.querySelectorAll(s)];
export const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

export const mean = (xs) => xs.reduce((a, b) => a + b, 0) / Math.max(xs.length, 1);
export const fmtS = (s) => (s ?? 0).toFixed(1) + 's';
// Wall-clock durations: 42s, 4m 05s, 1h 03m.
export function fmtDur(s) {
  if (s == null || !isFinite(s)) return '—';
  const t = Math.round(s), h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), sec = t % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return h ? `${h}h ${pad(m)}m` : m ? `${m}m ${pad(sec)}s` : `${sec}s`;
}
export const pct = (x) => (x == null || !isFinite(x) ? '—' : Math.round(x * 100) + '%');
export const fmtInt = (n) => (n == null ? '—' : Math.round(n).toLocaleString());
export const fmtDate = (ms) => (ms ? new Date(ms).toLocaleString(undefined, {dateStyle: 'medium', timeStyle: 'short'}) : '—');

export function fmtUSD(v) {
  if (v == null || !isFinite(v)) return '—';
  return '$' + (v >= 0.01 ? v.toFixed(2) : v === 0 ? '0' : v.toPrecision(2));
}

// Single-hue sequential tint for pass rates: plain surface at 0%, deepest green at 100%. The number carries the value.
export const passTint = (rate) => `color-mix(in oklab, rgb(var(--success)) ${Math.round((rate ?? 0) * 26)}%, rgb(var(--surface)))`;

export const spinner = '<span class="spinner" aria-hidden="true"></span>';
export const stat = (label, value, title = '') => `<div class="stat"${title ? ` title="${esc(title)}"` : ''}><div class="stat-label">${label}</div><div class="stat-value">${value}</div></div>`;
export const passBadge = (passed) => (passed ? '<span class="badge badge-pass">Pass</span>' : '<span class="badge badge-fail">Fail</span>');
export const empty = (html) => `<div class="empty">${html}</div>`;
export const loading = (what = 'Loading') => `<div class="flex items-center gap-2 p-6 text-sm text-ink-muted">${spinner}${esc(what)}…</div>`;
export const errorBox = (msg) => `<div class="notice notice-error">${esc(msg)}</div>`;
