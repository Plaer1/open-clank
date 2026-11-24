// Native theme canvases share the same frame, visibility and reduced-motion
// scheduler as graphics consumers. Geometry changes are local to this mount.
import { createSceneOwner } from './scene-owner.js';

// Keep full-viewport decoration within one 16 MiB RGBA surface per layer.
// CSS coordinates stay in viewport units even on ultrawide and Retina screens.
export function backgroundBackingScale(width, height, pixelRatio) {
  return Math.min(pixelRatio || 1, 2, 4096 / Math.max(1, width, height),
    Math.sqrt((4 * 1024 * 1024) / Math.max(1, width * height)));
}

export function mountBackgroundCanvas(options, lifecycle) {
  const {canvas, bodyClass, resize, paint, resizeTarget, onLayoutChange,
    onDispose, onResizeStart, onResizeSettled} = options;
  let elapsed = 0;
  let resizeTimer = 0;
  let observer = null;
  let disposed = false;
  let geometry = '';
  let geometryDirty = true;

  function updateGeometry() {
    if (!geometryDirty) return;
    geometryDirty = false;
    const key = [window.innerWidth, window.innerHeight, window.devicePixelRatio].join('/');
    if (geometry === key) return;
    geometry = key;
    resize();
  }

  function onResize() {
    onResizeStart?.();
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (disposed) return;
      // Resizing clears the canvas. Perform that work in the paint callback.
      geometryDirty = true;
      onResizeSettled?.();
      owner.invalidate();
    }, 96);
  }

  const owner = createSceneOwner({
    id: canvas.id,
    maxFps: options.maxFps || 30,
    isAlive() {
      if (canvas.isConnected && document.body.classList.contains(bodyClass)) return true;
      dispose();
      return false;
    },
    scene: { advance(delta) { elapsed += delta; } },
    paint(time, reduced) {
      updateGeometry();
      paint(reduced ? 0 : elapsed, reduced);
    },
  });

  function dispose() {
    if (disposed) return;
    disposed = true;
    clearTimeout(resizeTimer);
    window.removeEventListener('resize', onResize);
    observer?.disconnect();
    owner.dispose();
    lifecycle.clearScene();
    onDispose?.();
    lifecycle.release(dispose);
    canvas.remove();
  }

  lifecycle.acquire(dispose, () => owner.invalidate());
  canvas.dataset.backgroundEffectCanvas = 'true';
  canvas.__disposeEffect = dispose;
  canvas.__requestBackgroundRepaint = () => owner.invalidate();
  window.addEventListener('resize', onResize);
  if (resizeTarget && typeof ResizeObserver === 'function') {
    observer = new ResizeObserver(() => onLayoutChange?.());
    observer.observe(resizeTarget);
  }
  owner.start();
}
