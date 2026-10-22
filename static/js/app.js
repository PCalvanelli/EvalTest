// Entry point: load config, wire the views into the router, theme toggle.

import {$, store} from './ui.js';
import {app} from './config.js';
import {getJSON} from './api.js';
import {register, route} from './router.js';
import {initRun} from './run.js';
import {renderEconomics} from './econ.js';
import {initDrawer, closeTraceDrawer, showTraces, invalidateTraceCache} from './traces.js';
import {showHistory} from './history.js';
import {showLabels} from './labels.js';

const THEMES = ['auto', 'light', 'dark'];
function applyTheme(theme) {
  if (theme === 'auto') { delete document.documentElement.dataset.theme; store.del('theme'); }
  else { document.documentElement.dataset.theme = theme; store.set('theme', theme); }
  $('#themeToggle').textContent = `Theme: ${theme[0].toUpperCase()}${theme.slice(1)}`;
  if (!$('#view-run').hidden) renderEconomics();  // chart colors come from tokens; redraw for the new mode
}

async function init() {
  applyTheme(store.get('theme') ?? 'auto');
  $('#themeToggle').onclick = () => {
    const now = document.documentElement.dataset.theme ?? 'auto';
    applyTheme(THEMES[(THEMES.indexOf(now) + 1) % THEMES.length]);
  };
  initDrawer();

  const cfg = await getJSON('/api/config');
  app.config = cfg;
  initRun(cfg);
  if (cfg.mlflow?.enabled && cfg.mlflow.ui_url) {
    const nav = $('#mlflowNav');
    nav.href = cfg.mlflow.embedded ? '/mlflow/' : cfg.mlflow.ui_url;
    nav.classList.remove('hidden');
  }

  register('run', (_, {changedView}) => { if (changedView) renderEconomics(); });
  register('history', () => showHistory());
  register('traces', (params, {changedView}) => { if (changedView) invalidateTraceCache(); showTraces(params); });
  register('labels', (params) => showLabels(params));
  window.addEventListener('hashchange', () => { closeTraceDrawer(); route(); });
  route();
}

init();
