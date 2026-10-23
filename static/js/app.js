// Entry point: load config and wire the views into the router.

import {$} from './ui.js';
import {app} from './config.js';
import {getJSON} from './api.js';
import {register, route} from './router.js';
import {initRun, showRun} from './run.js';
import {showHistory} from './history.js';
import {showHarness} from './harness.js';

async function init() {
  const cfg = await getJSON('/api/config');
  app.config = cfg;
  initRun(cfg);
  if (cfg.mlflow_url) {
    $('#mlflowNav').href = `${cfg.mlflow_url}/`;
    $('#mlflowNav').classList.remove('hidden');
  }

  register('run', showRun);
  register('history', (params) => showHistory(params));
  register('harness', (params) => showHarness(params));
  window.addEventListener('hashchange', route);
  route();
}

init();
