// Original Open Clank SVG family. Static geometry; palette comes from the owner.
// Small navigation silhouettes omit the extra facets used by file artwork.
const body = d => `<path class="oc-icon-body" d="${d}" fill="var(--oc-icon-body,currentColor)"/>`;
const detail = d => `<path class="oc-icon-detail" d="${d}" fill="none"/>`;
const ink = d => `<path class="oc-icon-ink" d="${d}" fill="none"/>`;
const shine = d => `<path class="oc-icon-highlight" d="${d}" fill="none"/>`;
const shade = d => `<path class="oc-icon-shade" d="${d}" stroke="none"/>`;
const dot = (x,y,r=1.25,foreground=false) => `<circle class="${foreground ? 'oc-icon-ink-dot' : 'oc-icon-dot'}" cx="${x}" cy="${y}" r="${r}" fill="currentColor" stroke="none"/>`;
const plate = (d, mark, large=false) => body(d) + detail(mark) + (large ? shine('M5 6h7') : '');
const rounded = 'M6 3.5h12a3 3 0 0 1 3 3v11a3 3 0 0 1-3 3H6a3 3 0 0 1-3-3v-11a3 3 0 0 1 3-3Z';
const page = 'M7 2.5h7.5L20 8v11.5a2 2 0 0 1-2 2H7a2.5 2.5 0 0 1-2.5-2.5V5A2.5 2.5 0 0 1 7 2.5Z';
const fold = detail('M14.5 3v4a1.5 1.5 0 0 0 1.5 1.5h3.5');
const sheet = (mark, large) => body(page) + fold + detail(mark) + (large ? shine('M7 6v10') + shade('M7 19h11v1H7Z') : '');
const folder = (open, large) => body('M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z') +
  (open ? body('M5.5 10.5H21a1 1 0 0 1 1 1.3L19.8 19a2.5 2.5 0 0 1-2.4 2H4a1.4 1.4 0 0 1-1.4-1.7l1.4-7.3a1.5 1.5 0 0 1 1.5-1.5Z') : detail('M4 10h16')) +
  (large ? shine(open ? 'M6.5 13h11.5' : 'M6 12h12') + shade('M5 18h14l-.6 2H5Z') : shine('M6 7h2'));
const star = 'M12 3.1c.5 0 2.7 5.3 3.1 5.6l6.1 1c.6.2-4 4.2-4.3 4.7l.9 6c0 .7-5.3-2.7-5.8-2.7s-5.8 3.4-5.8 2.7l.9-6C6.8 14 2.2 9.9 2.8 9.7l6.1-1c.4-.3 2.6-5.6 3.1-5.6Z';
const clock = large => body('M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z') + detail('M12 7v5l3 2') + (large ? shine('M6 9a6 6 0 0 1 4-3') : '');
const database = large => body('M3.5 6c0-2 3.8-3.5 8.5-3.5s8.5 1.5 8.5 3.5v12c0 2-3.8 3.5-8.5 3.5S3.5 20 3.5 18Z') + detail('M4 6c0 2 3.5 3.5 8 3.5S20 8 20 6M4 12c2 3 14 3 16 0') + (large ? shine('M6.5 11v6') : '');
const envelope = large => body('M5 5h14a2.5 2.5 0 0 1 2.5 2.5v10A2.5 2.5 0 0 1 19 20H5a2.5 2.5 0 0 1-2.5-2.5v-10A2.5 2.5 0 0 1 5 5Z') + detail('m3.5 7 7 5.5a2.5 2.5 0 0 0 3 0l7-5.5') + (large ? shine('M6 7h10') + detail('m4 17 4-4m12 4-4-4') : '');
const calendar = large => plate('M5 5h14a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2Z','M4 10h16M8 3v4m8-4v4M8 14h2m4 0h2m-8 3h2',large);
const badge = (mark,large) => plate(rounded,mark,large);
const code = large => badge('m9 8-4 4 4 4m6-8 4 4-4 4m-2-9-2 10',large);
const shapes = {
  file: large => sheet('',large), text: large => sheet('M8 12h8m-8 4h6',large),
  document: large => sheet('M8 12h8m-8 4h6',large),
  folder: large => folder(false,large), 'folder-open': large => folder(true,large),
  'folder-plus': large => folder(false,large) + detail('M12 13v5m-2.5-2.5h5'),
  'file-plus': large => sheet('M12 12v6m-3-3h6',large),
  image: large => body('M5 3.5h14a2 2 0 0 1 2 2v13a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-13a2 2 0 0 1 2-2Z') + detail('m4 17 5-5 4 4 3-3 4 5') + dot(8,8,1.6) + (large ? shine('M6 5.5h10') : ''),
  video: large => badge('m10 8 6 4-6 4Z',large) + (large ? detail('M4 7h1m14 0h1M4 17h1m14 0h1') : ''),
  audio: large => body('M9 17V6a1 1 0 0 1 .8-1l9-2A1 1 0 0 1 20 4v12a3.5 3.5 0 1 1-2-3V8l-7 1.5V18a3.5 3.5 0 1 1-2-1Z') + (large ? shine('M11 6.5 17 5') : ''),
  archive: large => plate('M4 7h16v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2ZM3 3h18v5H3Z','M9 12h6',large),
  spreadsheet: large => badge('M4 9h16M9 4v16m0-6h11m-5-5v11',large),
  presentation: large => plate('M4 4h16a1 1 0 0 1 1 1v12H3V5a1 1 0 0 1 1-1Z','m7 13 3-4 3 3 4-5M12 17v4m-4 0h8',large),
  database, font: large => badge('m7 17 5-10 5 10M9 13h6',large),
  executable: large => badge('m7 8 4 4-4 4m7 0h3',large), terminal: large => badge('m7 8 4 4-4 4m7 0h3',large),
  volume: large => plate('M5 3.5h14l2 12v4.5H3V15.5Z','M4 15h16m-13 3h2m7 0h1',large),
  workspace: large => badge('M8 4v16m0-11h12',large),
  gallery: large => body('M2.5 6.5h15v14h-15Z') + body('M6.5 3.5h15v14h-15Z') + detail('m8 14 4-4 3 3 3-4 2 5') + dot(11,7,1.2) + (large ? shine('M9 5.5h9') : ''),
  library: large => body('M3 4h5v16H3ZM9.5 4h5v16h-5ZM16 5l4-1 2.5 15-4 1Z') + detail('M4 8h3m4-1h2m5 2 2-.5') + (large ? shine('M5 12v4m7-5v5') : ''),
  home: large => body('m2.5 10 8-7a2 2 0 0 1 3 0l8 7-2 2v8.5H14V14h-4v6.5H4.5V12Z') + (large ? shine('m6 9 6-5') + detail('M16 16v2') : ''),
  locations: large => shapes.volume(large), computer: large => plate('M4 3.5h16v13H4Z','M4 13h16m-8 4v3m-4 0h8',large),
  chat: large => body('M6 3.5h12a3 3 0 0 1 3 3v9a3 3 0 0 1-3 3h-8l-6 3v-4a3 3 0 0 1-1-2v-9a3 3 0 0 1 3-3Z') + detail('M7 9h10m-10 4h6') + (large ? shine('M6 6h11') : ''),
  email: envelope, inbox: envelope, calendar,
  memory: large => body('M10 3C7 1.5 4 4 4.5 7 1.5 9 2 12 4.5 13c-1.5 4 1 7 4 6 0 3 3.5 3 3.5 0 0 3 3.5 3 3.5 0 3 1 5.5-2 4-6 2.5-1 3-4 0-6 .5-3-2.5-5.5-5.5-4Z') + detail('M12 4v15M5 8c3 0 4 2 4 4m-4 2c2-1 4 0 4 2m10-8c-3 0-4 2-4 4m4 2c-2-1-4 0-4 2') + (large ? shine('M7 5.5 6 7') : ''),
  tasks: large => badge('m6 9 2 2 3-4m2 2h5m-12 7 2 2 3-4m2 2h5',large),
  usage: large => badge('M7 17v-4m5 4V7m5 10v-7',large),
  research: large => body('M8 3h8v2l-1.5 1v5l5.5 7.5c1 1.5 0 3-1.5 3h-13c-1.5 0-2.5-1.5-1.5-3L9 11V6L8 5Z') + detail('M7 16h10m-6-7h2') + (large ? shine('m7 18 2-3') + dot(14,18,1) : ''),
  compare: large => body('M3 4h7v16H3Zm11 0h7v16h-7Z') + detail('m5 10 3 2-3 2m14-4-3 2 3 2') + (large ? shine('M5 6h3m8 0h3') : ''),
  cookbook: large => body('M4 3.5h13a3 3 0 0 1 3 3v14H6a3 3 0 0 1-3-3v-12A2 2 0 0 1 4 3.5Z') + detail('M6 4v14m-2 0h15M10 8h6m-6 4h4') + (large ? shine('M9 6h6') : ''),
  timeline: large => badge('M6 7h12M6 12h12M6 17h12m-8-13v16',large),
  graph: large => body('M3 4h6v6H3Zm12 10h6v6h-6Z') + ink('m9 7 9 10m0-10-9 10') + body('M15 4h6v6h-6ZM3 14h6v6H3Z'),
  treehouse: large => body('m12 2-7 7h3l-5 6h7v6h4v-6h7l-5-6h3Z') + (large ? shine('m10 7 2-2') + detail('M8 12h7') : ''),
  model: large => body('m12 2 9 5v10l-9 5-9-5V7Z') + detail('m4 7 8 5 8-5m-8 5v8') + (large ? shine('m7 6 5-2') : ''),
  provider: large => body('M7 3h10v6a5 5 0 0 1-4 5v7h-2v-7a5 5 0 0 1-4-5Z') + ink('M9 2v4m6-4v4') + (large ? shine('M9 8v2') : ''),
  skills: large => body('M12 2c3 3 3 5 2 7 3-1 4-3 4-3 4 6 3 13-3 15C6 24 1 15 6 9c0 3 2 5 3 4-2-4 0-7 3-11Z') + detail('M12 14c-3 3-2 6 1 6s4-3 1-5') + (large ? shine('M8 15v2') : ''),
  hex: large => body('m12 2 9 5v10l-9 5-9-5V7Z') + detail('M8 8h8v8H8Z') + (large ? shine('m6 8 6-3') : ''),
  agents: large => plate('M5 6h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2Z','M12 3v3m-5 6h1m8 0h1m-9 4h8',large),
  help: large => body('M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z') + detail('M9 9a3 3 0 0 1 6 0c0 2-3 2-3 4m0 3h.01') + (large ? shine('M6 9a6 6 0 0 1 4-3') : ''),
  json: large => badge('M9 6c-3 0-2 5-4 6 2 1 1 6 4 6m6-12c3 0 2 5 4 6-2 1-1 6-4 6',large),
  code, markdown: large => badge('M6 16V8l4 5 4-5v8m4-8v8m-2-2 2 2 2-2',large),
  csv: large => shapes.spreadsheet(large), pdf: large => sheet('M8 17v-6h3a2 2 0 0 1 0 4H8m6-4v6h3',large),
  python: large => badge('M8 7h6a3 3 0 0 1 0 6h-4a3 3 0 0 0 0 6h6M8 7v2m8 8v2',large),
  javascript: large => badge('M10 8v7c0 3-4 3-4 0m11-6c-4-3-6 2-2 3 5 1 3 7-2 4',large),
  typescript: large => badge('M5 8h8m-4 0v9m9-8c-4-3-6 2-2 3 5 1 3 7-2 4',large),
  html: code, xml: code, css: large => badge('M9 7 7 17m10-10-2 10M6 10h13M5 14h13',large),
  yaml: large => badge('M9 7h9m-6 5h6m-6 5h6',large) + dot(6,7) + dot(9,12) + dot(9,17),
  bash: large => shapes.terminal(large), sh: large => shapes.terminal(large), sql: database,
  svg: large => badge('M7 8h4v4H7Zm7 0 4 4h-4Zm-4 8a2 2 0 1 0 4 0 2 2 0 0 0-4 0Z',large),
  rust: large => body('m9 3 1.5 2h3L15 3l3 2-.5 2.5 1.5 2.5 2.5.5v3l-2.5.5-1.5 2.5L18 19l-3 2-1.5-2h-3L9 21l-3-2 .5-2.5L5 14l-2.5-.5v-3L5 10l1.5-2.5L6 5Z') + detail('M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z') + (large ? shine('m9 7 2-1') : ''),
  go: large => badge('M10 8a4 4 0 1 0 0 8m0-4H8m10 0a3 4 0 1 1-6 0 3 4 0 0 1 6 0Z',large),
  java: large => body('M5 10h12v7a3 3 0 0 1-3 3H8a3 3 0 0 1-3-3Z') + ink('M17 12h1a2.5 2.5 0 0 1 0 5h-1M8 3c-2 2 2 3 0 5m5-5c-2 2 2 3 0 5') + (large ? shine('M7 12v4') : ''),
  c: large => badge('M16 8a6 6 0 1 0 0 8',large),
  cpp: large => badge('M10 8a4 5 0 1 0 0 8m4-6v4m-2-2h4m3-2v4m-2-2h4',large),
  csharp: large => badge('M10 8a4 5 0 1 0 0 8m5-9-2 10m6-10-2 10m-5-7h8m-9 4h8',large),
  ruby: large => body('m3 8 4-5h10l4 5-9 13Z') + detail('M4 8h16M8 8l4 12 4-12M8 8l4-4 4 4') + (large ? shine('M7 6h2') : ''),
  php: large => badge('M6 16v-7h3a2 2 0 0 1 0 4H6m6-4v7m0-4h3m0-3v7m3 0v-7h2a2 2 0 0 1 0 4h-2',large),
  mermaid: large => shapes.graph(large),
};
const actions = {
  back:'m14 5-7 7 7 7', forward:'m10 5 7 7-7 7', up:'m5 11 7-7 7 7M12 5v15',
  'chevron-right':'m9 6 6 6-6 6', 'chevron-down':'m6 9 6 6 6-6', 'chevron-left':'m15 6-6 6 6 6', 'chevron-up':'m6 15 6-6 6 6',
  refresh:'M20 10a8 8 0 1 0 0 6m0-13v7h-7', restore:'M4 10a8 8 0 1 1 0 6m0-13v7h7M12 7v5l3 2',
  close:'m6 6 12 12M18 6 6 18', add:'M12 4v16M4 12h16', remove:'M5 12h14', check:'m4 12 5 5L20 6',
  download:'M12 3v12m-5-5 5 5 5-5M4 18v3h16v-3', upload:'M12 16V4m-5 5 5-5 5 5M4 18v3h16v-3',
  undo:'m8 5-5 5 5 5M4 10h10a6 6 0 0 1 6 6v3', redo:'m16 5 5 5-5 5M20 10H10a6 6 0 0 0-6 6v3',
  reply:'m9 5-6 6 6 6M4 11h9a7 7 0 0 1 7 7v2', send:'m3 4 18 8-18 8 4-8Zm4 8h13',
  attachment:'m9 8 6-6a4 4 0 0 1 6 6L10 19a5 5 0 0 1-7-7L14 2m-8 14 10-10',
  cut:'M8 8 20 20M8 16 20 4M9 7a3 3 0 1 1-6 0 3 3 0 0 1 6 0Zm0 10a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z',
  menu:'M4 6h16M4 12h16M4 18h16', filter:'M3 4h18l-7 8v7l-4 2v-9Z',
  sort:'M4 6h12M4 12h8m-8 6h4m11-12v13m-3-3 3 3 3-3', 'sort-alpha':'m3 11 3-7 3 7m-5-3h4M3 15h6l-6 6h6m8-15v13m-3-3 3 3 3-3',
  list:'M9 6h12M9 12h12M9 18h12M3 6h1m-1 6h1m-1 6h1', details:'M3 6h18M3 12h18M3 18h18M8 4v16',
  'split-right':'M12 4v16', 'split-below':'M4 12h16',
  expand:'M14 3h7v7m0-7-7 7M10 21H3v-7m0 7 7-7', collapse:'M21 3l-7 7m0-7v7h7M3 21l7-7m-7 0h7v7',
  pause:'M8 5v14m8-14v14', link:'m9 15 6-6M8 14l-2 2a3 3 0 0 0 4 4l3-3m-2-10 3-3a3 3 0 0 1 4 4l-2 2',
  external:'M13 3h8v8m0-8L11 13M8 4H4v16h16v-4', enter:'M20 4v7a4 4 0 0 1-4 4H4m5-5-5 5 5 5',
  move:'M12 3v18M3 12h18m-12-6 3-3 3 3m-6 12 3 3 3-3M6 9l-3 3 3 3m12-6 3 3-3 3',
};
for (const [key,path] of Object.entries(actions)) shapes[key] = () => ink(path);
Object.assign(shapes, {
  search: large => body('M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0Z') + detail('M14 10a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z') + ink('m16 16 5 5') + (large ? shine('M6 7 7 6') : ''),
  more: () => dot(12,5,2,true) + dot(12,12,2,true) + dot(12,19,2,true),
  star: large => body(star) + (large ? shine('m10 10 2-4') : ''), 'star-filled': large => body(star) + detail('m8 12 3 3 5-6'),
  bookmark: large => body('M6 3h12v18l-6-4-6 4Z') + (large ? shine('M8 6h6') : ''), 'bookmark-filled': large => shapes.bookmark(large) + detail('m8 10 3 3 5-6'),
  clock, recent: clock, recents: clock,
  edit: large => body('m4 16 12-12a2 2 0 0 1 3 0l1 1a2 2 0 0 1 0 3L8 20l-5 1Z') + detail('m14 6 4 4M4 16l4 4') + (large ? shine('m7 15 7-7') : ''),
  copy: large => body('M3 3h13v14H3Z') + body('M8 8h13v13H8Z') + (large ? shine('M11 11h7') : ''),
  paste: large => body('M5 5h14v16H5Z') + body('M8 3h8v5H8Z') + detail('M9 12h6m-6 4h6') + (large ? shine('M7 11v6') : ''),
  save: large => badge('M8 4v5h8V4M7 20v-7h10v7',large),
  trash: large => body('M6 7h12l-1 14H7Z') + ink('M4 6h16M9 6V3h6v3') + detail('M10 10v7m4-7v7') + (large ? shine('M8 9v7') : ''),
  bell: large => body('M6 10a6 6 0 0 1 12 0c0 5 2 7 2 7H4s2-2 2-7Z') + ink('M10 20h4') + (large ? shine('M8 9a4 4 0 0 1 2-3') : ''),
  settings: large => shapes.rust(large),
  grid: large => body('M3 3h7v7H3Zm11 0h7v7h-7ZM3 14h7v7H3Zm11 0h7v7h-7Z'),
  columns: large => body('M3 4h5v16H3Zm7 0h5v16h-5Zm7 0h4v16h-4Z'),
  preview: large => badge('M13 4v16M6 9h3m-3 4h3',large),
  'split-right': large => badge(actions['split-right'],large), 'split-below': large => badge(actions['split-below'],large),
  eye: large => body('M2 12c5-10 15-10 20 0-5 10-15 10-20 0Z') + detail('M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z'),
  'eye-off': large => shapes.eye(large) + ink('M3 3 21 21'),
  play: large => body('M6 3.5 21 12 6 20.5Z') + (large ? shine('m8 7 6 3.5') : ''), stop: () => body('M5 4h14a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Z'),
  lock: large => plate('M5 10h14v11H5Z','M8 10V7a4 4 0 0 1 8 0v3M12 14v3',large),
  unlock: large => plate('M5 10h14v11H5Z','M8 10V7a4 4 0 0 1 8 0M12 14v3',large),
  info: large => body('M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z') + detail('M12 11v6m0-10h.01') + (large ? shine('M6 9a6 6 0 0 1 4-3') : ''),
  warning: large => body('M10.5 3.5a1.8 1.8 0 0 1 3 0L22 19c.5 1-.3 2-1.5 2h-17C2.3 21 1.5 20 2 19Z') + detail('M12 9v5m0 3h.01') + (large ? shine('m6 15 4-7') : ''),
  error: large => body('m8 2.5-5.5 5.5v8L8 21.5h8l5.5-5.5V8L16 2.5Z') + detail('m8 8 8 8m0-8-8 8') + (large ? shine('M7 7 9 5') : ''),
  success: large => body('M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z') + detail('m7 12 3 3 7-7') + (large ? shine('M6 9a6 6 0 0 1 4-3') : ''),
  loading: () => ink('M20 12a8 8 0 1 1-3-6M20 3v7h-7'),
  unavailable: large => sheet('m8 17 8-7',large), select: () => body('M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z') + detail('M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z'),
  dice: large => badge('M7 7h.01m10 0h.01m-5 5h.01m-5 5h.01m10 0h.01',large),
  sparkles: large => body('m12 2 3 7 7 3-7 3-3 7-3-7-7-3 7-3Z') + (large ? shine('m11 10 1-3') : ''),
  location: large => body('M12 22s-8-8-8-13a8 8 0 0 1 16 0c0 5-8 13-8 13Z') + detail('M15 9a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z'),
  shared: large => shapes.agents(large),
  dot: () => `<circle cx="12" cy="12" r="6" fill="currentColor" stroke="none"/>`,
  'dot-outline': () => ink('M20 12a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z'),
  unchecked: large => body(rounded),
  'reply-all': () => ink('m7 6-5 6 5 6m5-12-5 6 5 6M8 12h7a6 6 0 0 1 6 6'),
  network: large => body('M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z') + detail('M4 12h16M12 3c-5 4-5 14 0 18 5-4 5-14 0-18Z'),
  key: large => body('M12 15a5 5 0 1 1-10 0 5 5 0 0 1 10 0Z') + ink('m11 11 9-9m-4 4 3 3'),
  contact: large => body('M15 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0ZM3 21v-3c0-6 16-6 16 0v3Z'),
  translate: large => badge('M5 8h7m-4-3v3m-3 6c4-1 6-4 6-6m-6 2 6 4m2 3 3-7 3 7m-5-2h4',large),
  activity: () => ink('M2 12h4l3-8 6 16 3-8h4'),
  'text-size': () => ink('M4 7V4h16v3M12 4v16m-4 0h8'),
  'text-scale': () => ink('M10 3v18M3 10l3-3 3 3M6 7v10m-3-3 3 3 3-3M15 7h6m-3 0v10'),
  highlight: large => body('m14 3 7 7-9 9-7-7Z') + ink('m5 12-3 5 5-2M2 21h8') + (large ? shine('m10 10 4-4') : ''),
  shield: large => body('M12 2 21 6v6c0 5-9 10-9 10S3 17 3 12V6Z') + detail('m7 12 3 3 7-7') + (large ? shine('m6 7 5-2') : ''),
  brightness: large => body('M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z') + detail('M12 4v16') + shade('M12 4a8 8 0 0 1 0 16Z'),
  hue: large => body('M13 8a5 5 0 1 1-10 0 5 5 0 0 1 10 0Z') + body('M21 9a5 5 0 1 1-10 0 5 5 0 0 1 10 0Z') + body('M18 17a5 5 0 1 1-10 0 5 5 0 0 1 10 0Z') + detail('M10 7h.01m5 9h.01'),
  levels: large => badge('M6 17v-4m4 4V8m4 9v-6m4 6V6',large),
  balance: large => body('M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z') + detail('M12 4v16M4 12h16') + shade('M12 4a8 8 0 0 1 8 8h-8ZM4 12h8v8a8 8 0 0 1-8-8Z'),
  swap: () => ink('M3 7h17m-4-4 4 4-4 4M21 17H4m4-4-4 4 4 4'),
  lightbulb: large => body('M9 21h6v-6a7 7 0 1 0-6 0Z') + detail('M9 17h6M12 11v5') + (large ? shine('M7 8a5 5 0 0 1 3-3') : ''),
  sliders: large => ink('M3 6h18M3 12h18M3 18h18') + body('M7 3h3v6H7Zm7 6h3v6h-3ZM6 15h3v6H6Z'),
  microphone: large => body('M8 6a4 4 0 0 1 8 0v6a4 4 0 0 1-8 0Z') + ink('M5 10v2a7 7 0 0 0 14 0v-2M12 19v3m-4 0h8') + (large ? shine('M10 5v5') : ''),
  keyboard: large => badge('M6 8h.01m4 0h.01m4 0h.01m4 0h.01M6 12h.01m4 0h.01m4 0h.01m4 0h.01M8 16h8',large),
  smiley: large => body('M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z') + detail('M8 9h.01m8 0h.01M8 14c2 3 6 3 8 0'),
  sad: large => body('M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z') + detail('M8 9h.01m8 0h.01M8 16c2-3 6-3 8 0'),
  neutral: large => body('M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z') + detail('M8 9h.01m8 0h.01M8 15h8'),
  learn: large => body('m2 9 10-6 10 6-10 6Z') + ink('M6 12v6c4 3 8 3 12 0v-6'),
});
const aliases = Object.freeze({ favorites:'star-filled', favorite:'star-filled', images:'gallery', documents:'library', workspaces:'workspace',
  history:'restore', rename:'edit', delete:'trash', symlink:'link', 'arrow-left':'back', 'arrow-right':'forward', 'arrow-up':'up',
  'arrow-down':'download', on:'success', complete:'success', saved:'success', failed:'error', denied:'unavailable', offline:'unavailable',
  revoked:'unavailable', pending:'loading', uploading:'upload', downloading:'download', success:'success' });
const roles = Object.freeze({ inherit:'currentColor', accent:'var(--accent-primary,var(--accent,currentColor))',
  success:'var(--color-success,var(--green,#72ba48))', info:'var(--color-info,var(--color-link-hover,#438dd5))',
  warning:'var(--color-warning,var(--warn,#d9a325))', danger:'var(--color-danger,var(--color-error,#e0635d))',
  muted:'var(--color-muted,currentColor)' });
const statusRole = Object.freeze({ success:'success', info:'info', warning:'warning', error:'danger', unavailable:'inherit', sparkles:'accent' });
export const UI_ICON_IDS = Object.freeze(Object.keys(shapes));
export function iconId(id) { const key = String(id || '').trim().toLowerCase(); return aliases[key] || key; }
export function hasUiIcon(id) { return Object.hasOwn(shapes,iconId(id)); }
function attr(value) { return String(value).replace(/[&<>"']/g,char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char])); }
let styleInstalled = false;
function installStyle() {
  if (styleInstalled || typeof document === 'undefined') return;
  const href = new URL('./uiIcons.css',import.meta.url).href;
  if (!document.querySelector('link[data-oc-icons]')) {
    const link = document.createElement('link'); link.rel = 'stylesheet'; link.href = href;
    link.dataset.ocIcons = ''; (document.head || document.documentElement).appendChild(link);
  }
  styleInstalled = true;
}
/** SVG markup. Owner labels its button; pass label only for a meaningful standalone image. */
export function uiIcon(id, size = 16, options = {}) {
  installStyle();
  const opts = options && typeof options === 'object' ? options : {};
  const key = iconId(id), draw = shapes[key] || shapes.file;
  // Account/status dots intentionally render below the usual 8px control size.
  const numericSize = Math.max(1,Math.min(256,Number(size) || 16));
  const large = opts.variant === 'artwork' || (opts.variant !== 'small' && numericSize > 24);
  const role = Object.hasOwn(roles,opts.role) ? opts.role : (statusRole[key] || 'inherit');
  const color = role === 'inherit' ? '' : `color:${roles[role]};`;
  const cls = `oc-icon oc-icon--${large ? 'artwork' : 'small'}${opts.className ? ' ' + opts.className : ''}`;
  const style = color + (opts.style || '');
  const label = opts.label ? `role="img" aria-label="${attr(opts.label)}"` : 'aria-hidden="true"';
  let inner = (opts.label ? `<title>${attr(opts.label)}</title>` : '') + draw(large);
  const state = iconId(opts.state);
  if (state && shapes[state] && state !== key) {
    const stateColor = roles[statusRole[state] || (['download','upload','loading'].includes(state) ? 'info' : 'inherit')];
    inner += `<g class="oc-icon-state" transform="translate(12.5 12.5) scale(.5)" style="color:${stateColor}"><circle class="oc-icon-state-ground" cx="12" cy="12" r="12" fill="var(--panel,var(--bg,#fff))" stroke="none"/>${shapes[state](false)}</g>`;
  }
  return `<svg${opts.id ? ` id="${attr(opts.id)}"` : ''} class="${attr(cls)}"${style ? ` style="${attr(style)}"` : ''} width="${numericSize}" height="${numericSize}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" ${label} focusable="false" data-icon="${attr(shapes[key] ? key : 'file')}">${inner}</svg>`;
}
/** Mount semantic static SVG slots in place, preserving owner nodes and listeners. */
export function mountUiIcons(root = document) {
  const slots = [...(root.querySelectorAll?.('svg[data-ui-icon]') || [])];
  if (root.matches?.('svg[data-ui-icon]')) slots.unshift(root);
  for (const slot of slots) {
    const template = slot.ownerDocument.createElement('template');
    template.innerHTML = uiIcon(slot.dataset.uiIcon, slot.getAttribute('width') || 16, {
      className: slot.getAttribute('class') || '', style: slot.getAttribute('style') || '',
      role: slot.dataset.uiIconRole || 'inherit',
      label: slot.getAttribute('aria-hidden') !== 'true' ? slot.getAttribute('aria-label') : '',
    });
    const rendered = template.content.firstElementChild;
    slot.innerHTML = rendered.innerHTML;
    for (const attribute of rendered.attributes) slot.setAttribute(attribute.name, attribute.value);
    slot.removeAttribute('data-ui-icon'); slot.removeAttribute('data-ui-icon-role');
  }
}
/** A control message stays text; only its fixed UI symbol is rendered as markup. */
export function setUiIconText(node, id, text, size = 13) {
  if (!node) return;
  node.textContent = text;
  const slot = node.ownerDocument.createElement('span');
  slot.innerHTML = uiIcon(id, size, { role:'inherit', style:'margin-right:4px;' });
  node.prepend(slot);
}
export default uiIcon;
