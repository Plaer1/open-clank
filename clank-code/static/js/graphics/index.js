/**
 * Graphics substrate public surface.
 *
 * S23 delivers the shared capability/lifecycle adapters used by backgrounds,
 * Graph and Imps. WebGL2 is the accelerated backend; Canvas2D is the ordinary
 * fallback and is always sufficient for the UI. Scene state never depends on
 * GPU resources, and exactly one scene owner runs per surface.
 *
 * Rain (S24) and LCARS (S25) are intentionally out of scope here.
 */

export {
  BACKEND_WEBGL2,
  BACKEND_CANVAS2D,
  DEFAULT_LIMITS,
  resolveLimits,
  probeGraphicsCapability,
  watchContextLoss,
} from './capability.js';

export {
  createBoundedCache,
  createGlyphAtlas,
  createResourceBudget,
  clampCanvasAllocation,
} from './resources.js';

export { createCanvas2DBackend } from './backend-canvas2d.js';
export { createWebGL2Backend } from './backend-webgl2.js';
export { createGraphicsScene, createDrawBatch } from './scene-state.js';
export {
  createSceneOwner,
  getSceneOwner,
  countSceneOwners,
} from './scene-owner.js';
export { createGraphicsConsumer } from './consumer.js';
