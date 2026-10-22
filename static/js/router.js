// Hash router: #/<view>?<params>. Views register a show(params) function; go() switches synchronously, so a
// caller can render into a view right after switching to it (charts need the view visible to measure width).

const views = {};
let current = null;

export const register = (name, show) => { views[name] = show; };
export const parse = () => {
  const [path, query = ''] = location.hash.replace(/^#\/?/, '').split('?');
  return {name: views[path] ? path : 'run', params: Object.fromEntries(new URLSearchParams(query))};
};

export function route() {
  const {name, params} = parse();
  for (const el of document.querySelectorAll('.view')) el.hidden = el.id !== `view-${name}`;
  for (const a of document.querySelectorAll('[data-tab]')) {
    if (a.dataset.tab === name) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  }
  const changedView = name !== current;
  current = name;
  views[name](params, {changedView});
}

/** Navigate without waiting for the hashchange event. */
export function go(hash) {
  history.pushState(null, '', hash);
  route();
}
