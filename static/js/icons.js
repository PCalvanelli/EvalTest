// Small icons beside target and model names. dbt and Malloy show their own marks, unchanged except scaled, at the same
// size as every other icon (static/icons/SOURCES.md has the files' sources and terms; the footer carries the notice).
// Raw SQL, the glossary and models get simple line icons drawn here, in the text color so they follow the theme.

const line = (paths) => `<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.4"
  stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" class="inline-block h-4 w-4 shrink-0 align-[-3px] text-ink-muted">${paths}</svg>`;
const mark = (file) => `<img src="/static/icons/${file}" alt="" width="16" height="16" aria-hidden="true"
  class="inline-block h-4 w-4 shrink-0 object-contain align-[-3px]">`;

const TARGETS = {
  raw: line('<ellipse cx="8" cy="3.5" rx="5" ry="2"/><path d="M3 3.5v9c0 1.1 2.2 2 5 2s5-.9 5-2v-9"/><path d="M3 8c0 1.1 2.2 2 5 2s5-.9 5-2"/>'),
  glossary: line('<path d="M3 2.5h7.5a1.5 1.5 0 0 1 1.5 1.5v9.5H4.5A1.5 1.5 0 0 1 3 12z"/><path d="M3 12a1.5 1.5 0 0 1 1.5-1.5H12"/><path d="M6 5.5h3.5"/>'),
  metricflow: mark('dbt-bit-standalone.png'),
  malloy: mark('malloydata-avatar.png'),
};
const SPARK = line('<path d="M8 2c.35 3.3 2.7 5.65 6 6-3.3.35-5.65 2.7-6 6-.35-3.3-2.7-5.65-6-6 3.3-.35 5.65-2.7 6-6z"/>');

/** A target's icon (empty for one without), to put before its name. */
export const targetIcon = (id) => TARGETS[id] ?? '';
/** The icon before a model's name. */
export const modelIcon = () => SPARK;
/** Icon and escaped-by-caller name side by side, kept on one line. */
export const withIcon = (icon, nameHtml) => (icon ? `<span class="inline-flex items-center gap-1.5 whitespace-nowrap">${icon}${nameHtml}</span>` : nameHtml);
