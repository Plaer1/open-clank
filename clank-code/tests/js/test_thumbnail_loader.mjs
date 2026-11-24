import assert from 'node:assert/strict';
import test from 'node:test';

import { createThumbnailLoader } from '../../static/js/editor/thumbnailLoader.js';
import {loadedStillImage} from '../../static/js/graphics/mediaImageAuthority.js';
import {syntheticPng} from '../fixtures/s00_ui_fixture.mjs';

const pngBlob = () => new Blob([new Uint8Array([137, 80, 78, 71])], { type: 'image/png' });
const image = () => ({ isConnected: true, dataset: {}, src: '' });

test('thumbnail loader coalesces same-key requests and reuses cache after rerender', async () => {
  const calls = [];
  let urls = 0;
  const loader = createThumbnailLoader({
    fetchImpl: async (url) => { calls.push(url); return { ok: true, status: 200, async blob() { return pngBlob(); } }; },
    urlApi: { createObjectURL: () => `blob:${++urls}`, revokeObjectURL() {} },
  });
  const first = image();
  const second = image();
  loader.attach(first, { key: 'owner:host:file:1', url: '/thumb/1', renderFallback() {} });
  loader.attach(second, { key: 'owner:host:file:1', url: '/thumb/1', renderFallback() {} });
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(calls, ['/thumb/1']);
  assert.equal(first.src, 'blob:1');
  assert.equal(second.src, 'blob:1');
  loader.detachAll();
  const third = image();
  loader.attach(third, { key: 'owner:host:file:1', url: '/thumb/1', renderFallback() {} });
  assert.equal(third.src, 'blob:1');
  assert.equal(loadedStillImage(third),null,'Unknown/truncated PNG does not gain still-image metadata on a cache hit');
  assert.deepEqual(calls, ['/thumb/1']);
  loader.clear();
});

test('authorized still key/source survive network paint then detached rerender cache paint without refetch',async()=>{
 let calls=0,urls=0;const still=()=>new Blob([syntheticPng(2,2)],{type:'image/png'});
 const loader=createThumbnailLoader({fetchImpl:async()=>{calls++;return{ok:true,status:200,blob:async()=>still()};},urlApi:{createObjectURL:()=>`blob:still-${++urls}`,revokeObjectURL(){}}});
 const key='synthetic-owner:host:file:revision',first=image(),detach=loader.attach(first,{key,url:'/synthetic-thumbnail'});
 await new Promise(resolve=>setTimeout(resolve,0));assert.deepEqual(loadedStillImage(first),{key,source:first.src});
 detach();assert.equal(loadedStillImage(first),null);first.isConnected=false;loader.detachAll();
 const replacement=image();loader.attach(replacement,{key,url:'/synthetic-thumbnail'});
 assert.deepEqual(loadedStillImage(replacement),{key,source:replacement.src});assert.equal(replacement.src,'blob:still-1');assert.equal(calls,1);assert.equal(urls,1);
 replacement.src='blob:different-binding';assert.equal(loadedStillImage(replacement),null,'Source drift never retains the cached presentation binding');loader.clear();
});

test('detached stale network result cannot replace current image identity or metadata',async()=>{
 const responses=new Map();let urls=0;
 const loader=createThumbnailLoader({fetchImpl:url=>new Promise(resolve=>responses.set(url,resolve)),urlApi:{createObjectURL:()=>`blob:identity-${++urls}`,revokeObjectURL(){}}});
 const img=image(),oldDetach=loader.attach(img,{key:'old-identity',url:'/old-synthetic'});oldDetach();loader.attach(img,{key:'current-identity',url:'/current-synthetic'});
 const response=()=>({ok:true,status:200,blob:async()=>new Blob([syntheticPng(2,2)],{type:'image/png'})});
 responses.get('/current-synthetic')(response());await new Promise(resolve=>setTimeout(resolve,0));const current=loadedStillImage(img);assert.equal(current.key,'current-identity');
 responses.get('/old-synthetic')(response());await new Promise(resolve=>setTimeout(resolve,0));assert.deepEqual(loadedStillImage(img),current);assert.equal(img.src,current.source);assert.equal(urls,1);loader.clear();
});

test('thumbnail loader retries transient failures but bounds concurrency', async () => {
  let active = 0;
  let peak = 0;
  const attempts = new Map();
  const loader = createThumbnailLoader({
    fetchImpl: async (url) => {
      active += 1;
      peak = Math.max(peak, active);
      await new Promise(resolve => setTimeout(resolve, 1));
      active -= 1;
      const count = (attempts.get(url) || 0) + 1;
      attempts.set(url, count);
      if (count < 2) return { ok: false, status: 503, async blob() { return pngBlob(); } };
      return { ok: true, status: 200, async blob() { return pngBlob(); } };
    },
    urlApi: { createObjectURL: (blob) => `blob:${blob.size}:${Math.random()}`, revokeObjectURL() {} },
  });
  const imgs = Array.from({ length: 8 }, (_, index) => image());
  imgs.forEach((img, index) => loader.attach(img, { key: `owner:file:${index}`, url: `/thumb/${index}`, renderFallback() {} }));
  await new Promise(resolve => setTimeout(resolve, 750));
  assert.ok(peak <= 4);
  assert.equal(attempts.size, 8);
  assert.ok([...attempts.values()].every(value => value === 2));
  assert.ok(imgs.every(img => img.src.startsWith('blob:')));
  loader.clear();
});
