#!/usr/bin/env node
/** Production color geometry at batch boundaries; recorded GL uploads, not GPU performance. */
import assert from 'node:assert/strict';
import test from 'node:test';
import { createWebGL2Backend } from '../static/js/graphics/backend-webgl2.js';

function recordingGL() {
  const bindings = new Map();
  const triangles = [];
  const draws = [];
  const gl = {
    VERTEX_SHADER: 1, FRAGMENT_SHADER: 2, COMPILE_STATUS: 3, LINK_STATUS: 4,
    ARRAY_BUFFER: 5, ELEMENT_ARRAY_BUFFER: 6, DYNAMIC_DRAW: 7, FLOAT: 8,
    TRIANGLES: 9, UNSIGNED_SHORT: 10, DEPTH_TEST: 11, BLEND: 12,
    SRC_ALPHA: 13, ONE_MINUS_SRC_ALPHA: 14, ONE: 15,
    createShader: () => ({}), shaderSource() {}, compileShader() {},
    getShaderParameter: () => true, deleteShader() {},
    createProgram: () => ({}), attachShader() {}, linkProgram() {},
    getProgramParameter: () => true, deleteProgram() {},
    getUniformLocation: () => ({}), createVertexArray: () => ({}),
    bindVertexArray() {}, deleteVertexArray() {},
    createBuffer: () => ({}), deleteBuffer() {},
    bindBuffer(target, buffer) { bindings.set(target, buffer); },
    bufferData(target, bytes) { bindings.get(target).capacity = bytes; },
    bufferSubData(target, offset, values) {
      const buffer = bindings.get(target);
      assert.equal(offset, 0);
      assert.ok(values.byteLength <= buffer.capacity, 'upload fits allocated buffer');
      buffer.values = values.slice(); // Snapshot before the producer reuses its arrays.
    },
    enableVertexAttribArray() {}, vertexAttribPointer() {}, useProgram() {},
    uniform2f() {}, viewport() {}, disable() {}, enable() {}, blendFuncSeparate() {},
    drawElements(mode, count, type, offset) {
      assert.equal(mode, gl.TRIANGLES);
      assert.equal(type, gl.UNSIGNED_SHORT);
      assert.equal(offset, 0);
      const vertices = bindings.get(gl.ARRAY_BUFFER).values;
      const indices = bindings.get(gl.ELEMENT_ARRAY_BUFFER).values;
      assert.equal(vertices.length % 6, 0);
      assert.equal(count, indices.length, 'every submitted index is uploaded');
      assert.equal(count % 3, 0);
      const vertexCount = vertices.length / 6;
      assert.ok(vertexCount <= 4096);
      for (const index of indices) {
        assert.ok(index < vertexCount,
          `index ${index} must address this batch's ${vertexCount} vertices`);
      }
      for (let i = 0; i < indices.length; i += 3) {
        triangles.push(Array.from(indices.slice(i, i + 3), index =>
          Array.from(vertices.slice(index * 6, index * 6 + 6))));
      }
      draws.push({ vertexCount, indexCount: count });
    },
  };
  return { gl, triangles, draws };
}

const vertex = (x, y, rgb) => [Math.fround(x), Math.fround(y), ...rgb, 1];
function rectGeometry(rect) {
  const a = vertex(rect.x, rect.y, [1, 0, 0]);
  const b = vertex(rect.x + rect.w, rect.y, [1, 0, 0]);
  const c = vertex(rect.x + rect.w, rect.y + rect.h, [1, 0, 0]);
  const d = vertex(rect.x, rect.y + rect.h, [1, 0, 0]);
  return [[a, b, c], [a, c, d]];
}

function circleGeometry(circle) {
  const segments = Math.max(8, Math.min(32, Math.ceil(circle.r * 1.5) + 8));
  const point = (radius, i, rgb) => vertex(
    circle.x + Math.cos(i * Math.PI * 2 / segments) * radius,
    circle.y + Math.sin(i * Math.PI * 2 / segments) * radius, rgb);
  const center = vertex(circle.x, circle.y, [0, 1, 0]);
  const triangles = [];
  for (let i = 0; i < segments; i += 1) {
    triangles.push([center, point(circle.r, i, [0, 1, 0]), point(circle.r, i + 1, [0, 1, 0])]);
  }
  if (circle.stroke) {
    const inner = Math.max(0, circle.r - circle.strokeWidth / 2);
    const outer = circle.r + circle.strokeWidth / 2;
    for (let i = 0; i < segments; i += 1) {
      const a = point(inner, i, [0, 0, 1]);
      const b = point(outer, i, [0, 0, 1]);
      const c = point(outer, i + 1, [0, 0, 1]);
      const d = point(inner, i + 1, [0, 0, 1]);
      triangles.push([a, b, c], [a, c, d]);
    }
  }
  return triangles;
}

function checkGeometry(rectCount, circles, expectMultipleDraws = true) {
  const recorder = recordingGL();
  const backend = createWebGL2Backend({ canvas: { width: 800, height: 600 }, gl: recorder.gl });
  const rects = Array.from({ length: rectCount }, (_, i) =>
    ({ x: i * 3, y: -10, w: 1, h: 2, color: '#f00' }));
  backend.beginFrame();
  backend.drawRects(rects);
  backend.drawCircles(circles);
  backend.endFrame();
  const expected = [...rects.flatMap(rectGeometry), ...circles.flatMap(circleGeometry)];
  assert.equal(recorder.triangles.length, expected.length, 'no dropped or duplicated triangles');
  for (let t = 0; t < expected.length; t += 1) {
    for (let v = 0; v < 3; v += 1) {
      for (let c = 0; c < 6; c += 1) {
        const actual = recorder.triangles[t][v][c];
        const wanted = expected[t][v][c];
        // Independent angle arithmetic can differ below Float32 precision.
        assert.ok(Math.abs(actual - wanted) <= Math.max(1e-5, Math.abs(wanted) * 1e-6),
          `triangle ${t}, vertex ${v}, component ${c}: ${actual} != ${wanted}`);
      }
    }
  }
  assert.equal(recorder.draws.length > 1, expectMultipleDraws);
  return recorder.draws;
}

const circle = (i, r = 4, stroke = false) => ({
  x: i * 64, y: 32, r, color: '#0f0',
  ...(stroke ? { stroke: '#00f', strokeWidth: 2 } : {}),
});

test('circle fan reserves its entire primitive before crossing a mixed batch', () => {
  checkGeometry(1023, [circle(0)]); // 4092 existing vertices, 16-vertex circle.
});

test('circle fan can exactly fill a batch then start the next at zero', () => {
  const draws = checkGeometry(1020, [circle(0), circle(1)]);
  assert.equal(draws[0].vertexCount, 4096);
  assert.equal(draws[1].vertexCount, 16);
});

test('many circles preserve every triangle across repeated flushes', () => {
  checkGeometry(0, Array.from({ length: 1000 }, (_, i) => circle(i, 16)));
});

test('mixed small and stroked circles keep fill and outline geometry together', () => {
  checkGeometry(1021, Array.from({ length: 80 }, (_, i) => circle(i, i % 17, i % 2 === 0)));
});

test('small batch retains its geometry without an unnecessary flush', () => {
  checkGeometry(2, [circle(0), circle(1, 1, true)], false);
});
