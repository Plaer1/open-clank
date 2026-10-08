// Feature views register separately; shared integration imports each module once.
// Renderers receive the live, account-scoped context on every render.
const views = new Map();
const activityViews = new Map();
export function registerTreeHouseView(section, renderer) {
  if (typeof renderer !== 'function') throw new TypeError('TreeHouse renderer is required');
  views.set(section, renderer);
  return () => { if (views.get(section) === renderer) views.delete(section); };
}
export function renderTreeHouseView(section, root, context) {
  const renderer = views.get(section);
  if (!renderer) return false;
  renderer(root, context);
  return true;
}
export function registerTreeHouseActivity(type, renderer) {
  if (typeof renderer !== 'function') throw new TypeError('TreeHouse activity renderer is required');
  activityViews.set(type, renderer);
  return () => { if (activityViews.get(type) === renderer) activityViews.delete(type); };
}
export function renderTreeHouseActivity(root, context) {
  const renderer = activityViews.get(context.activity.activityType);
  if (!renderer) return false;
  renderer(root, context);
  return true;
}
export function loadTreeHouseStyles() {
  if (typeof document === 'undefined' || document.getElementById('treehouse-learning-styles')) return;
  const link = document.createElement('link');
  link.id = 'treehouse-learning-styles'; link.rel = 'stylesheet';
  link.href = new URL('./treehouseLearning.css', import.meta.url).href;
  document.head.append(link);
}
