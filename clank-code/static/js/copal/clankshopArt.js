// Original vector workshop and vehicles in the Open Clank palette.
import { ROOM_LABELS, STATE_TEXT } from './clankshopModel.js';

const INK = '#191A1E', CREAM = '#F4EAD2', GOLD = '#F6BE48', CORAL = '#FF776E', LILAC = '#B7A7E8';
export const VIEW_W = 960, VIEW_H = 640;
const esc = (value) => String(value).replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
const short = (value, max) => value.length > max ? value.slice(0, max - 1) + '…' : value;

export const ROOM_FLOOR = Object.freeze({
  eye: { x0: 387, x1: 573, floor: 154, cols: 3 },
  office: { x0: 58, x1: 456, floor: 486, cols: 4 },
  workshop: { x0: 504, x1: 902, floor: 486, cols: 4 },
  lamp: { x0: 60, x1: 446, floor: 606, cols: 5 },
  cot: { x0: 488, x1: 708, floor: 606, cols: 4 },
  fault: { x0: 718, x1: 900, floor: 606, cols: 3 },
});
export function slotPoint(room, slot) {
  const floor = ROOM_FLOOR[room] || ROOM_FLOOR.cot;
  const col = slot % floor.cols, row = Math.floor(slot / floor.cols);
  return { x: floor.x0 + (col + .5) * ((floor.x1 - floor.x0) / floor.cols), y: floor.floor - row * (room === 'office' || room === 'workshop' ? 86 : 43) };
}

const defs = () => `<defs>
  <pattern id="cs-floor" width="30" height="30" patternUnits="userSpaceOnUse"><path d="M0 0h30M0 0v30" fill="none" stroke="currentColor" opacity=".09"/></pattern>
  <pattern id="cs-rollers" width="16" height="20" patternUnits="userSpaceOnUse"><rect width="16" height="20" fill="#252830"/><path d="M3 0v20" stroke="#a99872" stroke-width="3" opacity=".5"/></pattern>
  <radialGradient id="cs-light"><stop stop-color="${GOLD}" stop-opacity=".28"/><stop offset="1" stop-color="${GOLD}" stop-opacity="0"/></radialGradient>
  <symbol id="cs-clank-body" viewBox="-28 -54 56 61" overflow="visible">
    <path d="M-12-9v14h9V-9m6 0v14h9V-9" fill="#726552" stroke="${INK}" stroke-width="2"/>
    <path d="M-15-24-22-12m37-12 7 12" stroke="currentColor" stroke-width="5" stroke-linecap="round"/>
    <rect x="-14" y="-30" width="28" height="22" rx="5" fill="currentColor" stroke="${INK}" stroke-width="3"/>
    <circle cy="-19" r="5" fill="${INK}" stroke="${CREAM}" stroke-width="1.4"/>
    <path d="M0-55 20-28H-20Z" fill="currentColor" stroke="${INK}" stroke-width="2.5" stroke-linejoin="round"/>
    <path d="M-10-36Q0-48 10-36Q0-25-10-36Z" fill="${INK}"/><circle cy="-36" r="2.9" fill="${CREAM}"/>
  </symbol>
  <symbol id="cs-desk" viewBox="0 0 108 76">
    <path d="M8 46h92v14H8zM16 60v15m75-15v15" stroke="#544b3e" stroke-width="4" fill="#b89562"/>
    <rect x="22" y="13" width="42" height="27" rx="4" fill="#303341" stroke="#a991bf" stroke-width="3"/>
    <path d="M28 21h14m-14 6h23m-23 6h11" stroke="#e5d8c0" stroke-width="2" opacity=".7"/>
    <path d="M43 40v6m27-.5 17-7m-10-16 8 9" stroke="#a991bf" stroke-width="3" stroke-linecap="round"/>
    <path d="M71 16h17l-5 10H69Z" fill="${GOLD}"/>
  </symbol>
  <symbol id="cs-press" viewBox="0 0 96 111">
    <path d="M11 102V18L23 5h50l12 13v84" fill="none" stroke="#695c55" stroke-width="12" stroke-linejoin="round"/>
    <path d="M16 102V18L26 9h44l12 9v84" fill="none" stroke="#bb8d72" stroke-width="6" stroke-linejoin="round"/>
    <rect x="37" y="13" width="22" height="42" rx="5" fill="#827ea5"/><path d="M48 45v23M32 68h32" stroke="${CREAM}" stroke-width="7"/>
    <path d="M6 103h84m-70-18h56" stroke="#a99872" stroke-width="9" stroke-linecap="round"/>
  </symbol>
</defs>`;
const plate = (x, y, label, faint = false) => `<text x="${x}" y="${y}" fill="var(--cs-scene-ink, ${CREAM})" font-size="${faint ? 12 : 16}" font-weight="700" letter-spacing="${faint ? 1.9 : 2.1}">${esc(label)}</text>`;
const rivets = (x, y) => `<path d="M${x} ${y}h.2m12 0h.2m12 0h.2" stroke="#ba9b69" stroke-width="4" stroke-linecap="round"/>`;
function backdrop() {
  return `<rect width="960" height="640" rx="18" fill="var(--cs-scene-base,#21252d)"/>
  <rect x="15" y="15" width="930" height="610" rx="20" fill="url(#cs-floor)" stroke="var(--cs-scene-outline,#535864)"/>
  <path d="M480 34 823 566H137Z" fill="none" stroke="#a98a52" stroke-opacity=".5" stroke-width="8" stroke-linejoin="round"/>
  <path d="M262 170 462 42Q480 29 498 42L698 170" fill="none" stroke="#f6be48" stroke-opacity=".6" stroke-width="2"/>
  <circle cx="480" cy="88" r="120" fill="url(#cs-light)"/><path d="M435 79Q480 38 525 79Q480 119 435 79Z" fill="${INK}" stroke="${GOLD}" stroke-width="4"/>
  <circle cx="480" cy="79" r="18" fill="${GOLD}"/><circle cx="480" cy="79" r="9" fill="${INK}"/><circle cx="476" cy="75" r="3" fill="${CREAM}"/>
  <path d="M344 158h272m-136 1v361" stroke="#806749" stroke-width="3" stroke-dasharray="7 11"/>
  <rect x="33" y="178" width="431" height="349" rx="18" fill="var(--cs-office,#302c37)" stroke="#9582a1" stroke-width="2"/>
  <rect x="496" y="178" width="431" height="349" rx="18" fill="var(--cs-factory,#39302b)" stroke="#a68460" stroke-width="2"/>
  <path d="M68 498h360m104 0h356" stroke="#8a775d" stroke-width="2" stroke-dasharray="6 8"/>
  <path d="M83 293h260m45 0h41m103 61h360" fill="none" stroke="#8a775d" stroke-width="3" opacity=".38"/>
  ${rivets(412,202)}${rivets(875,202)}${plate(55,207,'OFFICE')}${plate(518,207,'FACTORY')}
  <g opacity=".76">
    <rect x="55" y="228" width="373" height="78" rx="12" fill="#6a5c78" opacity=".38"/>
    <path d="M62 246h359m-353 38h348" stroke="#d1becf" stroke-opacity=".38" stroke-width="1.5"/>
    <use href="#cs-desk" x="76" y="289" width="108" height="76"/><use href="#cs-desk" x="191" y="289" width="108" height="76"/>
    <use href="#cs-desk" x="302" y="289" width="108" height="76"/>
    <path d="M79 392h324m-324 13h324" stroke="#856c59" stroke-width="2"/>
    <rect x="516" y="374" width="384" height="32" rx="8" fill="url(#cs-rollers)" stroke="#998879" stroke-width="2"/>
    <use href="#cs-press" x="544" y="241" width="96" height="111"/><use href="#cs-press" x="668" y="241" width="96" height="111"/>
    <use href="#cs-press" x="780" y="241" width="96" height="111"/>
    <path d="M538 225h350m-350 0v32m350-32v32" stroke="#8d7970" stroke-width="5" fill="none" stroke-linecap="round"/>
  </g>
  <path d="M435 467h88v28h-88Z" fill="url(#cs-rollers)" stroke="#998879" stroke-width="2"/>
  <rect x="34" y="543" width="414" height="85" rx="12" fill="var(--cs-office,#302c37)" stroke="#9582a1"/>
  <rect x="478" y="543" width="231" height="85" rx="12" fill="var(--cs-office,#302c37)" stroke="#9582a1"/>
  <rect x="716" y="543" width="210" height="85" rx="12" fill="var(--cs-factory,#39302b)" stroke="#a68460"/>
  ${plate(58,565,'ATTENTION',true)}${plate(499,565,'REST',true)}${plate(734,565,'SERVICE',true)}
  `;
}
export function clankArt(unit, phase = 'settled', track = null, hasOverlap = false) {
  const name = short(unit.name, 15), action = unit.currentAction || unit.lastAction || STATE_TEXT[unit.state] || '';
  const f = phase === 'charge' || phase === 'stamp' || phase === 'landing' || phase === 'settling';
  const detail = `${unit.name} · ${action} · ${ROOM_LABELS[unit.room] || ''}`;
  const color = /^#[a-f\d]{6}$/i.test(unit.colour) ? unit.colour : GOLD;
  return `<g class="cs-clank cs-state-${unit.state} cs-phase-${phase}" color="${color}">
    <title>${esc(detail)}</title><ellipse cy="4" rx="23" ry="6" fill="${INK}" opacity=".32"/>
    <use href="#cs-clank-body" x="-28" y="-55" width="56" height="61"/>
    ${['permission','question','failed'].includes(unit.state) ? `<circle cx="23" cy="-46" r="10" fill="${unit.state === 'failed' ? CORAL : GOLD}" stroke="${INK}" stroke-width="2"/><text x="23" y="-42" text-anchor="middle" fill="${INK}" font-weight="900" font-size="12">${unit.state === 'question' ? '?' : '!'}</text>` : ''}
    ${f ? `<g class="cs-stamp"><path d="M23-36h17v22H23Z" fill="${LILAC}" stroke="${INK}" stroke-width="2"/><path class="cs-ram" d="M31-26v9m-6 2h12" stroke="${CREAM}" stroke-width="3"/><circle class="cs-part" cx="38" cy="-8" r="5" fill="${GOLD}"/></g><path d="M-14 8h28" stroke="#7d7165" stroke-width="3"/><path d="M-14 8h${Math.min(28, (track?.charge || 0) * 7)}" stroke="${GOLD}" stroke-width="3"/>` : ''}
    ${hasOverlap ? '' : `<text x="0" y="19" text-anchor="middle" font-size="11" font-weight="800" stroke="${INK}" stroke-width="2.5" paint-order="stroke" fill="${CREAM}">${esc(name)}</text>
    <text x="0" y="31" text-anchor="middle" font-size="9.2" font-weight="650" stroke="${INK}" stroke-width="2.5" paint-order="stroke" fill="${CREAM}">${esc(short(action, 24))}</text>`}
  </g>`;
}
export function unitsSvg(placed) {
  return placed.map(unit => {
    const p = slotPoint(unit.room, unit.slot);
    return `<g transform="translate(${p.x} ${p.y})">${clankArt(unit)}</g>`;
  }).join('');
}

export const badgeSvg = extra => extra > 0 ? `<g transform="translate(816 43)"><rect width="98" height="24" rx="12" fill="${GOLD}"/><text x="49" y="16" text-anchor="middle" font-size="13" font-weight="700" fill="${INK}">+${extra} listed</text></g>` : '';
export function sceneSvg(placed = [], { extra = 0 } = {}) {
  return `<svg class="cs-scene" viewBox="0 0 ${VIEW_W} ${VIEW_H}" role="img" aria-label="Clank Workshop with Office, Factory, Attention, Rest and Service stations. Worker controls and status are listed beside the scene." font-family="system-ui,sans-serif">${defs()}${backdrop()}<g id="cs-units">${unitsSvg(placed)}</g><g id="cs-badge">${badgeSvg(extra)}</g></svg>`;
}

export const DRIFT_CAR = '<svg viewBox="-42 -46 84 89" aria-hidden="true"><path d="M-24-29h48l12 21 1 29-11 10h-51l-12-10V-8Z" fill="#14151b" opacity=".72" transform="translate(2 7)"/><path d="M-24-30h48l11 20v31l-10 8h-50l-10-8v-31Z" fill="#f6be48" stroke="#151922" stroke-width="3.5" stroke-linejoin="round"/><path d="M-19-20h38l7 16h-52Z" fill="#8e82bf" stroke="#151922" stroke-width="3"/><path d="M-26 10h52l-6 13h-40Z" fill="#3e375a"/><path d="M-30 15h7m46 0h7" stroke="#ff776e" stroke-width="5" stroke-linecap="round"/><path d="M-15-32h30" stroke="#f4ead2" stroke-width="3" stroke-linecap="round"/><path d="M-29 27v7m58-7v7" stroke="#171b22" stroke-width="6" stroke-linecap="round"/><path d="M-5 1H5L0 9Z" fill="#151922"/></svg>';
