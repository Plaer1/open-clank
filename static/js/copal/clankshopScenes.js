// Original Clankshop artwork. Scene geometry is independent of actor and turn state.
const ink = 'var(--clanker-outline,#0e0f12)';
const base = 'var(--bg,#191a1e)';
const well = 'var(--clanker-well,#15161a)';
const panel = 'var(--panel,#25272c)';
const raised = 'var(--clanker-raised,#34373e)';
const edge = 'var(--border,#555a62)';
const paper = 'var(--fg,#fff4d6)';
const gold = 'var(--accent-primary,#f6be48)';
const sky = 'var(--clanker-sky,#62c7e8)';
const coral = 'var(--color-error,#ff776e)';

function bolts(points) {
  return points.map(([x,y]) => `<circle cx="${x}" cy="${y}" r="3.5" fill="${ink}"/><path d="M${x-1.7} ${y+1.7}l3.4-3.4" stroke="${edge}" stroke-width="1.3"/>`).join('');
}

const stationArt = [
  // Optical gantry with scan bed and samples.
  `<path d="M13 78V26L34 7h131l18 19v52M31 72V31h134v41" fill="none" stroke="${sky}" stroke-width="9"/>
  <path d="M27 75h144v14H27zM37 73l20-15h86l20 15" fill="${raised}" stroke="${ink}" stroke-width="4"/>
  <path d="M83 25h34v14H83z" fill="${sky}"/><path d="M92 42 75 69h50l-17-27" fill="${sky}" opacity=".22"/>
  <path d="M67 63h15v10H67zm45 0h15v10h-15z" fill="${gold}" stroke="${ink}" stroke-width="2"/>`,
  // Twin assembly press with carriage, tooling and loose stamped flanges.
  `<path d="M17 86V18L29 6h142l12 12v68H17Z" fill="${raised}" stroke="${ink}" stroke-width="5"/>
  <path d="M28 28h144v47H28Z" fill="${well}"/>
  <path d="M52 19h29v34H52zm66 0h29v34h-29z" fill="${gold}" stroke="${ink}" stroke-width="3"/>
  <path d="M66 51v10m66-10v10M53 62h26m40 0h26" stroke="${paper}" stroke-width="5"/>
  <path d="M27 77h146m-134-6h49m21 0h49" stroke="${gold}" stroke-width="4"/>`,
  // Test rig with spoke meter and a wired workpiece.
  `<path d="M17 86V28L35 7h114l34 25v54Z" fill="${raised}" stroke="${ink}" stroke-width="5"/>
  <circle cx="62" cy="47" r="28" fill="${well}" stroke="${edge}" stroke-width="4"/>
  <path d="M43 54a20 20 0 0 1 39-5M62 49l11-12" fill="none" stroke="${sky}" stroke-width="3"/>
  <path d="M103 24h57v22h-57z" fill="${well}"/><path d="m109 36 9-5 8 10 9-13 16 7" fill="none" stroke="${gold}" stroke-width="2"/>
  <path d="M124 47v12q0 9 12 9h19m-15-8h18v18h-18z" fill="none" stroke="${sky}" stroke-width="4"/>`,
  // Parts bins and adjustable lift.
  `<path d="M15 53h170v11H15zm13 12v21h10V65m124 0v21h10V65" fill="${edge}" stroke="${ink}" stroke-width="3"/>
  <path d="M24 24h43l-7 27H32Zm52 0h43l-7 27H84Z" fill="${gold}" stroke="${ink}" stroke-width="3"/>
  <path d="M35 33h20m32 0h20M132 51l29-34m-30 0 29 34m-31-35h36" stroke="${sky}" stroke-width="5"/>`,
];

const stages = ['Inspection', 'Assembly', 'Calibration', 'Workbench'];
const bays = [
  ['Dispatch', 'M2 14h31m-11-9 11 9-11 9'],
  ['Attention', 'M18 2v15m0 5v3'],
  ['Recharge', 'M20 1 9 16h10l-3 12 13-16H18Z'],
  ['Repair', 'm5 24 13-13m-4-7 4 7 7 3 7-7M7 21l5 5'],
];

export function factoryBackdrop() {
  return `<rect width="960" height="640" rx="20" fill="${well}"/>
  <path d="M2 69 68 4h824l66 65v513l-55 56H56L2 582Z" fill="${panel}" stroke="${edge}" stroke-width="3"/>
  <path d="M23 62 64 22h832l41 40v37H23Z" fill="${raised}" stroke="${ink}" stroke-width="4"/>
  <path d="M26 98h908M32 45h126M802 45h126" stroke="${gold}" stroke-width="4"/>
  <text x="480" y="65" text-anchor="middle" fill="${paper}" font-size="27" font-weight="800" letter-spacing="4">CLANK WORKS</text>
  ${bolts([[41,76],[919,76],[24,626],[936,626]])}
  <path d="M22 113h916v34H22z" fill="${well}" stroke="${ink}" stroke-width="4"/>
  ${Array.from({length:47},(_,i) => `<path d="M${24+i*20} 118v23" stroke="${edge}" stroke-width="5"/>`).join('')}
  <path d="M24 145h912m-834-23 8 8-8 8m228-16 8 8-8 8m228-16 8 8-8 8m228-16 8 8-8 8" stroke="${gold}" stroke-width="3" fill="none"/>
  ${stages.map((name,i) => {
    const x = 24 + i*232;
    return `<g transform="translate(${x} 150)">
      <path d="M0 12 12 0h192l12 12v264l-14 14H14L0 276Z" fill="${base}" stroke="${edge}" stroke-width="3"/>
      <path d="M0 24h216m-200 69h184M9 185h198m-198 82h198" fill="none" stroke="${edge}" stroke-width="2" opacity=".55"/>
      <text x="13" y="18" fill="${paper}" font-size="13" font-weight="800" letter-spacing="1.5">${name.toUpperCase()}</text>
      <g transform="translate(46 29) scale(.63)">${stationArt[i]}</g>
      ${bolts([[201,13],[204,275],[12,275]])}
    </g>`;
  }).join('')}
  <path d="M23 449h914v18H23z" fill="${raised}" stroke="${ink}" stroke-width="3"/>
  <path d="M31 458h898" stroke="${sky}" stroke-width="2" stroke-dasharray="12 7"/>
  ${bays.map(([label,glyph],i) => `<g transform="translate(${24+i*232} 478)">
    <path d="M2 15 16 1h183l16 14v113l-14 14H14L2 128Z" fill="${well}" stroke="${edge}" stroke-width="3"/>
    <path d="M13 28h189M15 126h185" stroke="${edge}" stroke-width="2"/>
    <path d="${glyph}" transform="translate(177 7) scale(.62)" fill="none" stroke="${i===1 ? gold : sky}" stroke-linecap="round" stroke-width="3"/>
    <text x="13" y="21" fill="${paper}" font-size="13" font-weight="800" letter-spacing="1.4">${label.toUpperCase()}</text>
  </g>`).join('')}`;
}

export function factoryRobot({colour, phase, room, state}) {
  const shell = /^#[a-f\d]{6}$/i.test(colour) ? colour : gold;
  const resting = ['idle', 'unavailable', 'done'].includes(state);
  const charging = phase === 'charge';
  const imprint = phase === 'stamp';
  const released = phase === 'landing';
  const toolAngle = charging ? -36 : imprint ? 32 : 0;
  const sensor = resting ? `<path d="M-7-49h14" stroke="${edge}" stroke-width="3"/>`
    : `<circle cy="-50" r="5.5" fill="${sky}"/><circle cx="-1.5" cy="-52" r="1.6" fill="${paper}" stroke="none"/>`;
  const tool = room === 'intake'
    ? `<path d="M17-34h11v16H17Z" fill="${sky}"/><path d="M20-30h5m-5 4h5" stroke="${ink}" stroke-width="1.5"/>`
    : `<path d="M18-34h9v11l-5 4-4-4Z" fill="${gold}"/><path d="M22-19v7" stroke="${paper}" stroke-width="3"/>`;
  return `<ellipse cy="-2" rx="25" ry="5" fill="${ink}" opacity=".42"/>
  <g stroke="${ink}" stroke-width="2.5" stroke-linejoin="round">
    <path d="M-16-17h11v12l-3 3h-11v-5zm21 0h11l3 10v5H8L5-5Z" fill="${edge}"/>
    <path d="M-13-39-21-34l-4 12 5 4 7-9m26-12 10 4 3 11-5 5-7-10" fill="${raised}"/>
    <path d="M-12-42h24l6 8-3 17-7 5H-8l-7-5-3-17Z" fill="${shell}"/>
    <path d="M-21-59-13-69H9l12 10v12l-9 8h-25l-8-8Z" fill="${shell}"/>
    <path d="M-15-57h30v10l-5 4h-20l-5-4Z" fill="${well}"/>
    <path d="M-5-66v-6h11" fill="none" stroke="${edge}" stroke-width="3"/>
    <circle cx="7" cy="-72" r="2.8" fill="${sky}"/>
    ${sensor}
    <path d="m-7-26 5 4 8-8" fill="none" stroke="${well}" stroke-width="3"/>
    ${resting ? '' : `<g transform="rotate(${toolAngle} 21 -29)">${tool}</g>`}
  </g>
  ${imprint || released ? `<path d="m23-13 3-4m3 7 4-1m-8 3 2 4" stroke="${gold}" stroke-width="2.5" stroke-linecap="round"/>` : ''}
  ${released ? driftPickupAtBench() : ''}`;
}

function driftPickupAtBench() {
  return `<path d="m21-10 7-4 7 4v6l-7 4-7-4Z" fill="${gold}" stroke="${ink}" stroke-width="2"/>`;
}

export function driftBackdrop() {
  return `<rect width="360" height="640" fill="${well}"/>
  <path d="M0 0h360v175H0Z" fill="${raised}"/>
  <path d="M0 110h104l32-52h95l35 52h94v105H0Z" fill="${panel}" stroke="${ink}" stroke-width="5"/>
  <path d="M128 145V88l10-13h84l10 13v57Z" fill="${well}" stroke="${gold}" stroke-width="4"/>
  <path d="M144 102h72v11h-72z" fill="${sky}" opacity=".86"/>
  <path d="M0 234 122 134h27L0 455Zm360 0L238 134h-27l149 321Z" fill="${raised}" stroke="${edge}" stroke-width="3"/>
  <path d="M0 290 129 145M360 290 231 145" stroke="${sky}" stroke-width="4" stroke-dasharray="17 10"/>
  <path d="M0 425 63 308m-63 202 34-78m326-7-63-117m63 202-34-78" stroke="${edge}" stroke-width="2"/>
  <path d="M0 135h56v37H0m360-37h-56v37h56" fill="${gold}" stroke="${ink}" stroke-width="3"/>
  <path d="m8 138 19 31m4-31 19 31m302-31-19 31m-4-31-19 31" stroke="${ink}" stroke-width="5"/>`;
}

export function driftCityBackdrop() {
  return `<defs>
    <linearGradient id="cs-mega-sky" x2="0%" y2="100%">
      <stop stop-color="#151726"/><stop offset=".61" stop-color="#4a3956"/>
      <stop offset="1" stop-color="#b76e77"/>
    </linearGradient>
    <pattern id="cs-city-lights" width="13" height="12" patternUnits="userSpaceOnUse">
      <path d="M2 3h3m3 5h2" stroke="#cf9a96" stroke-width="1.5"/>
    </pattern>
  </defs>
  <rect width="360" height="640" fill="#22202b"/>
  <rect width="360" height="160" fill="url(#cs-mega-sky)"/>
  <circle cx="224" cy="115" r="27" fill="#ffbd99"/>
  <path d="M196 117h56m-55 5h54m-52 5h47m-43 5h37" stroke="#a76879" stroke-width="2"/>
  <path d="M0 107l25 -16 32 10 28 -14 21 13 35 -10 29 14 22 -8 35 12 31 -17 22 7 26 -16 22 13 32 -18v83H0Z" fill="#504455"/>
  <defs><path id="cs-drift-skyline" d="M0 149V66h17v-7h13v8h11v36h13V82h8V64h13v15h9v37h14V98h17v21h15v-9h12v22h12v-8h11v16h12v-7h11v8h17v-17h12v-13h16v7h14V90h13v-8h13v21h15V74h15v-9h14v10h8v33h12V56h12v-7h12v100Z"/></defs>
  <use href="#cs-drift-skyline" fill="#343043"/>
  <use href="#cs-drift-skyline" fill="url(#cs-city-lights)" style="fill:url(#cs-city-lights);opacity:.42"/>
  <g fill="none" stroke="#211f2c" stroke-linecap="round">
    <path d="M7 153V44m-7 8h26M325 153V64m-11 5h23" stroke-width="3"/>
    <path d="M0 52q80 40 159 38t166 -21l35 -8M0 57q86 43 160 37t165 -21l35 -9" stroke-width="1"/>
  </g>
  <path d="M0 148h118l14 5h96l14 -5h118v10H0Z" fill="#332d3f"/>
  <path d="M0 150h118m124 0h118" stroke="#ef9ab3" stroke-width="2"/>`;
}

export function driftStorefronts() {
  const signs=['RADIO','TURBO','HI-FI','NITE'];
  const facades=signs.map((sign,i)=>{
    const h=[76,118,92,104][i], neon=['#ff80a8','#71e1ed','#ffa978','#d49df4'][i];
    const windows=Array.from({length:Math.floor((h-37)/13)},(_,floor)=>
      [12,26,40].map((x,j)=>`<rect x="${x}" y="${12-h+floor*13}" width="8" height="5" fill="${(j+floor+i)%3 ? '#ffe0aa' : '#41334c'}"/>`).join('')).join('');
    return `<g id="cs-facade-${i}" stroke-linejoin="round">
      <path d="M0 0v-${h}l9 -9h48v${h+8}Z" fill="#43394e" stroke="#211f30" stroke-width="2"/>
      <path d="M0 0v-${h}l9 -9v${h+9}Z" fill="#242235"/>${windows}
      <path d="M8 -25h48v9H8Z" fill="${neon}"/><path d="M15 -14h17V0H15Zm21 0h15v11H36Z" fill="#171d32"/>
      <path d="M8 -${h+8}h49M10 -2h47" stroke="#96869b" stroke-width="2"/>
      <path d="M16 -${h+9}v-13m-4 5h12" stroke="#60546e" stroke-width="2"/>
      <rect x="-2" y="-48" width="9" height="20" rx="1" fill="#252437" stroke="${neon}"/>
      <path d="M0 -43h5m-5 5h4m-4 5h5" stroke="${neon}"/>
      <path d="M13 -22h29m-29 3h20" stroke="#312a40"/><title>${sign}</title></g>`;
  }).join('');
  return `<defs>${facades}</defs>`+Array.from({length:18},()=>'<use href="#cs-facade-0"/>').join('');
}

export function driftCar() {
  return `<ellipse cy="21" rx="49" ry="18" fill="#13121d" opacity=".55"/>
    <g stroke="#171a26" stroke-width="3" stroke-linejoin="round">
      <path d="M-36 -20h11v49h-12zm62 0h11v49H26z" fill="#171821"/>
      <path d="M-25 -36l9 -9h30l12 8 12 26 6 8v29l-9 7h-70l-9 -8V0l8 -11Z" fill="#ede5d8"/>
      <path d="M-32 -13l11 -23 10 -5h24l12 7 9 22 -10 -4h-48Z" fill="#254054"/>
      <path d="M-18 -33l9 -5h18l12 7 4 10h-48Z" fill="#8ba4b4"/>
      <path d="M-29 -11l7 -9h44l7 9 -5 10h-48Z" fill="#30384b"/>
      <path d="M-40 3h80v10h-80Z" fill="var(--cs-livery,#75d8dc)"/>
      <path d="M-35 18h70v11l-8 4h-54l-8 -4Z" fill="#34333e"/>
      <path d="M-32 15h18v7h-19Zm46 0h18l1 7H14Z" fill="#ff6179" stroke-width="1.5"/>
      <path d="M-7 21H7v6H-7Z" fill="#ffeab8" stroke-width="1"/>
      <path d="M-26 3v-6m51 6v-6" stroke-width="4"/>
      <path d="M-42 -8l84 1 -2 5h-81Z" fill="#444054"/>
      <path d="M-47 -14h10v5h-7Zm84 0h10l-3 5h-7Z" fill="#ede5d8"/>
      <path d="M21 30h10v5H21Z" fill="#b7aab1" stroke-width="2"/>
    </g>
    <path d="M22 36l2 10 2 -5 3 2 1 -7Z" fill="#ffab67" opacity="var(--cs-exhaust,0)"/>`;
}

const pickupShapes = {
  coil: `<circle r="13" fill="${sky}" stroke="${ink}" stroke-width="3"/><path d="M-6 2q0-9 8-8 8 2 3 8-4 4-6 0" fill="none" stroke="${well}" stroke-width="3" stroke-linecap="round"/>`,
  cell: `<rect x="-9" y="-13" width="18" height="26" rx="4" fill="${paper}" stroke="${ink}" stroke-width="3"/><path d="M2-8-4 1h5l-3 7 8-10H0Z" fill="${coral}" stroke="${ink}" stroke-width="1.2"/>`,
  star: `<path d="M0-16 5-6l11 2-8 8 1 11L0 9l-10 6 2-11-8-8 11-2Z" fill="${gold}" stroke="${ink}" stroke-width="3" stroke-linejoin="round"/><circle r="4" fill="${paper}"/>`,
};
export const driftPickup = (kind) => pickupShapes[kind] || `<path d="m0-16 13 7v17L0 16-13 8V-9Z" fill="${gold}" stroke="${ink}" stroke-width="3"/>
  <path d="m0-9 7 4v9L0 9-7 4v-9Z" fill="${well}"/><path d="M0-4v8m-4-4h8" stroke="${paper}" stroke-width="2"/>`;

export const driftHazard = () => `<path d="M-20 9-16-12h32l4 21Z" fill="${coral}" stroke="${ink}" stroke-width="3"/>
  <path d="m-14 4 9-12m0 13 9-13m0 13 9-13M-20 11h40" stroke="${ink}" stroke-width="4"/>`;

export function idleHousing() {
  return `<svg viewBox="0 0 640 380" aria-hidden="true" focusable="false">
    <path d="M12 76 81 13h478l69 63v228l-69 63H81l-69-63Z" fill="${raised}" stroke="${ink}" stroke-width="7"/>
    <path d="M45 92 96 48h448l51 44v196l-51 44H96l-51-44Z" fill="${panel}" stroke="${edge}" stroke-width="3"/>
    <path d="M44 108 91 61h62M486 61h62l48 47M44 272l47 47h62m333 0h62l48-47" fill="none" stroke="${gold}" stroke-width="5"/>
    <path d="M65 124h78v132H65zm432 0h78v132h-78z" fill="${well}" stroke="${ink}" stroke-width="4"/>
    <path d="M81 137h46v102H81zm432 0h46v102h-46z" fill="${panel}" stroke="${edge}" stroke-width="2"/>
    <path class="cs-idle-input-signal" d="M89 232h30V145H89Z" fill="${sky}" opacity=".65"/>
    <path class="cs-idle-output-signal" d="M521 232h30V145h-30Z" fill="${gold}" opacity=".65"/>
    <path d="M158 114 236 36h168l78 78v152l-78 78H236l-78-78Z" fill="${well}" stroke="${ink}" stroke-width="7"/>
    <circle cx="320" cy="190" r="151" fill="${base}" stroke="${edge}" stroke-width="5"/>
    <path d="M279 19h82m-82 342h82" stroke="${sky}" stroke-width="5" stroke-linecap="round"/>
    ${bolts([[100,34],[540,34],[100,346],[540,346],[33,191],[607,191]])}
  </svg>`;
}
