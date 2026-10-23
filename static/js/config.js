// Constants shared across views, plus the single mutable app state object.

export const MODELS = [
  {id: 'claude-opus-5-5', name: 'Opus 5.5'},
  {id: 'claude-sonnet-5-5', name: 'Sonnet 5.5', checked: true},
  {id: 'claude-haiku-5-5', name: 'Haiku 5.5'},
];
export const modelName = (id) => MODELS.find((m) => m.id === id)?.name ?? id;
// Targets come from the server (/api/config), in its order.
export const contextIds = () => Object.keys(app.config.contexts);
export const contextName = (id) => app.config.contexts[id]?.name ?? id;
export const cellId = (model, harness, context) => `${model}|${harness ? 'harness' : 'no-harness'}|${context}`;
export const comboLabel = (c) => `${contextName(c.context)} · ${c.harness ? 'harness' : 'baseline'}`;
export function cellLabel(id) {
  const [model, harness, context] = id.split('|');
  return `${modelName(model)} · ${comboLabel({harness: harness === 'harness', context})}`;
}

// The results being shown on the Run tab: a live matrix, or one loaded from History.
//   cells: [{id, model, harness_on, context}], runs: {runNo: {cellId: report}}, errors: {cellId: msg}
//   prompts: question texts, in case order
//   mlflow: {matrix, cells: {cellId: url}} when tracked
//   runSeconds: {runNo: wall-clock seconds}, totalSeconds: the whole matrix, once finished
export const freshState = (extra = {}) => ({cells: [], repeats: 1, runs: {}, errors: {}, prompts: [], selected: null,
  selectedRun: null, mlflow: null, runSeconds: {}, totalSeconds: null, ...extra});
export const app = {state: freshState(), config: null};
