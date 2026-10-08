// Original vector assets; user artwork stays raster-only and account-local.
import { achievementOwner } from '../achievementProducer.js';
const ROOT = '/static/icons/treehouse-achievements/';
const MAX_PACK_BYTES = 4 * 1024 * 1024;
const MAX_ICON_BYTES = 192 * 1024;
const KEY = /^k[a-f0-9]{12}$/;
let manifestPromise;
const vectorCache = new Map();
const preferenceKey = account => `openclank:achievement-art:v1:${account}`;
function requireOwner(account) {
  if (!account || account !== achievementOwner()) throw new Error('The signed-in account changed.');
}
function installStyle() {
  if (document.querySelector('link[data-achievement-art-style]')) return;
  const link = document.createElement('link'); link.rel = 'stylesheet';
  link.href = new URL('./treehouseAchievementArt.css', import.meta.url).href;
  link.dataset.achievementArtStyle = ''; document.head.append(link);
}
export function achievementArtManifest() {
  if (!manifestPromise) manifestPromise = fetch(`${ROOT}manifest.json`, { credentials:'same-origin' }).then(async response => {
    if (!response.ok) throw new Error('Achievement artwork unavailable.');
    const manifest = await response.json();
    if (manifest.schemaVersion !== 1 || manifest.kitVersion !== '1' || manifest.size !== 256 || !manifest.icons) throw new Error('Unsupported artwork manifest.');
    for (const [key, icon] of Object.entries(manifest.icons)) {
      if (!KEY.test(key) || icon.src !== `${ROOT}${key}.svg` || icon.width !== 256 || icon.height !== 256) throw new Error('Invalid bundled artwork reference.');
    }
    if (manifest.fallback !== `${ROOT}fallback.svg` || manifest.concealed !== `${ROOT}concealed.svg` || manifest.download !== `${ROOT}mascot-kit.zip`) throw new Error('Invalid artwork kit reference.');
    return manifest;
  }).catch(error => { manifestPromise = null; throw error; });
  return manifestPromise;
}
function vectorSource(path) {
  if (!vectorCache.has(path)) vectorCache.set(path, fetch(path, { credentials:'same-origin' }).then(async response => {
    if (!response.ok) throw new Error('Artwork unavailable.');
    const xml = new DOMParser().parseFromString(await response.text(), 'image/svg+xml');
    const svg = xml.documentElement;
    const tags = new Set(['svg','style','g','path','circle','ellipse','rect','polygon','polyline','line']);
    if (svg.localName !== 'svg' || svg.getAttribute('viewBox') !== '0 0 256 256' || xml.querySelector('parsererror')) throw new Error('Invalid artwork.');
    for (const element of [svg,...svg.querySelectorAll('*')]) {
      if (!tags.has(element.localName)) throw new Error('Unsupported bundled artwork.');
      for (const attr of [...element.attributes]) {
        if (/^on|href/i.test(attr.name) || /url\(/i.test(attr.value)) throw new Error('Invalid artwork reference.');
        if (attr.name === 'id') element.removeAttribute('id');
      }
      if (element.localName === 'style' && /@|url\(/i.test(element.textContent)) throw new Error('Invalid artwork style.');
    }
    return svg;
  }).catch(error => { vectorCache.delete(path); throw error; }));
  return vectorCache.get(path);
}
export function currentAchievementArtPack(account = achievementOwner()) {
  requireOwner(account);
  try {
    const raw = localStorage.getItem(preferenceKey(account));
    if (!raw || raw.length > MAX_PACK_BYTES) return null;
    const pack = JSON.parse(raw);
    return pack.schemaVersion === 1 && pack.kitVersion === '1' && typeof pack.name === 'string' && pack.icons && typeof pack.icons === 'object' ? pack : null;
  } catch (_) { return null; }
}
function pngBytes(value) {
  if (typeof value !== 'string' || !/^data:image\/png;base64,[A-Za-z0-9+/]+={0,2}$/.test(value) || value.length > MAX_ICON_BYTES * 1.4) throw new Error('Use PNG artwork under 192 KiB per icon.');
  const bytes = Uint8Array.from(atob(value.slice(22)), ch => ch.charCodeAt(0));
  if (bytes.length > MAX_ICON_BYTES || bytes.length < 33 || ![137,80,78,71,13,10,26,10].every((value,i)=>bytes[i]===value)) throw new Error('Invalid PNG artwork.');
  const header = new DataView(bytes.buffer);
  if (header.getUint32(8) !== 13 || String.fromCharCode(...bytes.slice(12,16)) !== 'IHDR' || header.getUint32(16) !== 256 || header.getUint32(20) !== 256) throw new Error('Artwork must be a 256 × 256 PNG.');
  return bytes;
}
async function normalizePng(value) {
  const bytes = pngBytes(value);
  const url = URL.createObjectURL(new Blob([bytes], { type:'image/png' }));
  try {
    const image = new Image(); image.src = url; await image.decode();
    if (image.naturalWidth !== 256 || image.naturalHeight !== 256) throw new Error('Invalid artwork dimensions.');
    const canvas = document.createElement('canvas'); canvas.width = canvas.height = 256;
    const context = canvas.getContext('2d'); if (!context) throw new Error('Artwork decoder unavailable.');
    context.drawImage(image,0,0); const normalized = canvas.toDataURL('image/png'); pngBytes(normalized);
    return normalized;
  } finally { URL.revokeObjectURL(url); }
}
export async function applyAchievementArtPack(input, account = achievementOwner()) {
  requireOwner(account);
  const raw = typeof input === 'string' ? input : JSON.stringify(input);
  if (typeof raw !== 'string' || raw.length > MAX_PACK_BYTES) throw new Error('Artwork pack must be under 4 MiB.');
  const pack = JSON.parse(raw); const manifest = await achievementArtManifest();
  if (pack.schemaVersion !== 1 || pack.kitVersion !== '1' || typeof pack.name !== 'string' || !pack.name.trim() || pack.name.length > 80 || !pack.icons || Array.isArray(pack.icons) || typeof pack.icons !== 'object') throw new Error('Invalid artwork pack.');
  const entries = Object.entries(pack.icons);
  if (!entries.length || entries.length > 37) throw new Error('Include between 1 and 37 artwork entries.');
  const icons = Object.create(null);
  for (const [key,value] of entries) {
    if (!KEY.test(key) || !Object.hasOwn(manifest.icons,key)) throw new Error('Unknown artwork slot.');
    icons[key] = await normalizePng(value);
    requireOwner(account);
  }
  const normalized = { schemaVersion:1, kitVersion:'1', name:pack.name.trim(), icons };
  const serialized = JSON.stringify(normalized);
  if (serialized.length > MAX_PACK_BYTES) throw new Error('Decoded artwork pack is too large.');
  requireOwner(account);
  try { localStorage.setItem(preferenceKey(account), serialized); }
  catch (_) { throw new Error('Browser storage is full or unavailable; the previous artwork is preserved.'); }
  document.dispatchEvent(new CustomEvent('openclank:achievement-art-changed', { detail:{accountId:account} }));
  return { name:normalized.name, count:entries.length };
}
export function clearAchievementArtPack(account = achievementOwner()) {
  requireOwner(account); localStorage.removeItem(preferenceKey(account));
  document.dispatchEvent(new CustomEvent('openclank:achievement-art-changed', { detail:{accountId:account} }));
}
// This consumes the server's allowed presentation; never derives a key from an ID/title.
export async function mountAchievementArtwork(host, entry, { accountId = achievementOwner() } = {}) {
  installStyle(); host.classList.add('oc-achievement-art-slot');
  const request = {}; host._achievementArtRequest = request;
  const concealed = entry?.rarity !== 'normal' && !entry?.earned;
  const label = concealed ? 'Hidden achievement' : 'Achievement artwork';
  host.setAttribute('role','img'); host.setAttribute('aria-label',label);
  const current = () => host._achievementArtRequest === request && accountId === achievementOwner();
  try {
    const manifest = await achievementArtManifest(); if (!current()) return;
    const key = !concealed && KEY.test(entry?.iconKey || '') && Object.hasOwn(manifest.icons,entry.iconKey) ? entry.iconKey : null;
    const pack = accountId ? currentAchievementArtPack(accountId) : null;
    const custom = key && pack?.icons?.[key];
    if (custom) {
      try {
        pngBytes(custom);
        const image = new Image(); image.alt = ''; image.src = custom; await image.decode();
        if (!current()) return;
        if (image.naturalWidth !== 256 || image.naturalHeight !== 256) throw new Error('Invalid cached artwork.');
        host.replaceChildren(image); return;
      } catch (_) { /* Invalid cached user art falls back to the bundled vector. */ }
    }
    const path = concealed ? manifest.concealed : key ? manifest.icons[key].src : manifest.fallback;
    let svg;
    try { svg = await vectorSource(path); }
    catch (_) { svg = await vectorSource(manifest.fallback); }
    if (!current()) return;
    const copy = svg.cloneNode(true); copy.setAttribute('aria-hidden','true'); copy.setAttribute('focusable','false');
    host.replaceChildren(copy);
  } catch (_) {
    if (current()) { const fallback = document.createElement('span'); fallback.className = 'oc-achievement-art-offline'; fallback.textContent = concealed ? '?' : '☆'; fallback.setAttribute('aria-hidden','true'); host.replaceChildren(fallback); }
  }
}
