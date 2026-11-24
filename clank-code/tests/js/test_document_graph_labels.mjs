import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import { documentGraphLabelMetrics } from '../../static/js/copal/graphModel.js';

test('Document Graph labels retain 12 screen pixels through camera and viewport scales', () => {
  for (const scale of [7.6223/9, .1, .25, .5, 1, 2, 8, 100]) {
    const value = documentGraphLabelMetrics({ a:scale, b:0, c:0, d:scale });
    assert(Math.abs(value.fontSize * scale - 12) < 1e-10);
    assert(Math.abs(value.gap * scale - 5) < 1e-10);
    assert(Math.abs(value.baseline * scale - 4) < 1e-10);
  }
});
test('actual CTM axes handle rotation and nonuniform ancestor scaling', () => {
  const value = documentGraphLabelMetrics({ a:0, b:2, c:-.5, d:0 });
  assert.deepEqual(value, { fontSize:24, gap:2.5, baseline:8 });
});
test('disconnected or invalid transforms are deferred rather than inventing screen metrics', () => {
  for (const matrix of [null, {}, {a:0,b:0,c:0,d:0}, {a:1,b:0,c:0,d:NaN}, {a:Infinity,b:0,c:0,d:1}]) assert.equal(documentGraphLabelMetrics(matrix), null);
});
test('actual label callback writes screen-correct metrics without moving glyphs and ignores disposed mounts', () => {
  const source = fs.readFileSync(new URL('../../static/js/copal.js', import.meta.url), 'utf8');
  const start = source.indexOf('  function syncGraphLabels() {');
  const body = source.slice(start, source.indexOf('\n\n  // Inspector panel', start));
  const attributes = {}, label = { style:{}, setAttribute:(key,value) => { attributes[key]=value; } };
  const node = {id:'note',hub:false}, positions = new Map([['note',{x:500,y:325}]]), root={isConnected:true};
  let scale=.5;
  const svgEl={getScreenCTM:()=>({a:scale,b:0,c:0,d:scale})};
  const make = disposed => new Function('destroyed','root','svgEl','documentGraphLabelMetrics','nodeEls','positions',body+';return syncGraphLabels;')(disposed,root,svgEl,documentGraphLabelMetrics,[{el:{querySelector:()=>label},node}],positions);
  const sync=make(false);sync();assert.equal(label.style.fontSize,'24px');assert.deepEqual(attributes,{x:525,y:333});assert.deepEqual(positions.get('note'),{x:500,y:325});
  scale=2;sync();assert.equal(label.style.fontSize,'6px');assert.deepEqual(attributes,{x:517.5,y:327});
  scale=.25;root.isConnected=false;sync();assert.equal(label.style.fontSize,'6px');
  root.isConnected=true;make(true)();assert.equal(label.style.fontSize,'6px');
});
test('real Document Graph updates labels after camera, mount and resize, disconnects on close', () => {
  const source = fs.readFileSync(new URL('../../static/js/copal.js', import.meta.url), 'utf8');
  const graph = source.slice(source.indexOf('function graphSvg('), source.indexOf('\nfunction renderGalaxy('));
  assert.match(graph, /documentGraphLabelMetrics\(svgEl\.getScreenCTM\(\)\)/);
  assert.match(graph, /setAttribute\('viewBox',[^;]+; syncGraphLabels\(\)/);
  assert.match(graph, /queueMicrotask\(syncGraphLabels\)/);
  assert.match(graph, /new ResizeObserver\(syncGraphLabels\)/);
  assert.match(graph, /labelResizeObserver\?\.disconnect\(\)/);
  assert.match(graph, /removeEventListener\?\.\('resize', syncGraphLabels\)/);
  assert.match(graph, /if \(destroyed \|\| !root\.isConnected\) return/);
  assert.match(graph, /label\.style\.fontSize =/);
});
