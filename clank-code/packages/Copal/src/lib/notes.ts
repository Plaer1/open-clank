export interface VaultNoteEntry {
  path: string;
  name: string;
  suffix: string;
  size: number;
  mtime: number;
}

export interface VaultNote {
  path: string;
  name: string;
  suffix: string;
  content: string;
  mtime: number;
}

export interface ParsedFrontmatter {
  raw: string;
  values: Record<string, string | string[] | boolean | number>;
}

export interface ParsedMarkdownTask {
  line: number;
  indent: number;
  statusSymbol: string;
  status: 'pending' | 'done' | 'in-progress' | 'cancelled';
  text: string;
  cleanText: string;
  doneDate?: string;
  dueDate?: string;
  scheduledDate?: string;
  recurrence?: string;
  priority: 'low' | 'medium' | 'high';
  inlineFields: Record<string, string>;
}

export interface ParsedNote {
  frontmatter: ParsedFrontmatter | null;
  body: string;
  wikilinks: string[];
  embeds: ParsedEmbed[];
  tags: string[];
  tasks: ParsedMarkdownTask[];
  fencedBlocks: { lang: string; line: number }[];
  dataviewBlocks: ParsedQueryBlock[];
  taskQueryBlocks: ParsedQueryBlock[];
  pluginWarnings: string[];
  callouts: ParsedCallout[];
  footnotes: ParsedFootnote[];
  mathBlocks: ParsedMathBlock[];
  tables: ParsedMarkdownTable[];
  outline: ParsedOutlineItem[];
  inlineFields: Record<string, string[]>;
  hasTemplater: boolean;
}

export interface ParsedQueryBlock {
  lang: string;
  line: number;
  query: string;
  kind: 'dataview' | 'tasks' | 'unknown';
}

export interface ParsedCallout {
  line: number;
  kind: string;
  title: string;
  fold?: '+' | '-';
}

export interface ParsedEmbed {
  line: number;
  kind: 'wikilink' | 'image';
  target: string;
  alt?: string;
}

export interface ParsedFootnote {
  line: number;
  id: string;
  text: string;
}

export interface ParsedMathBlock {
  line: number;
  body: string;
}

export interface ParsedMarkdownTable {
  line: number;
  headers: string[];
  rows: string[][];
}

export interface ParsedOutlineItem {
  line: number;
  level: number;
  text: string;
  kind: 'heading' | 'list';
}

export interface IndexedVaultNote extends VaultNote {
  title: string;
  key: string;
  parsed: ParsedNote;
  base?: ParsedBaseFile;
  canvas?: ParsedCanvasFile;
}

export interface ParsedBaseFile {
  sourcePath: string;
  keys: string[];
  values: Record<string, unknown>;
  filters: string[];
  columns: string[];
  unknownLines: string[];
}

export interface ParsedCanvasFile {
  sourcePath: string;
  nodes: { id: string; type?: string; label: string; file?: string }[];
  edges: { id: string; from: string; to: string; label?: string }[];
}

export interface VaultDerivedTask {
  id: string;
  title: string;
  sourcePath: string;
  noteTitle: string;
  line: number;
  status: ParsedMarkdownTask['status'];
  priority: ParsedMarkdownTask['priority'];
  dueDate?: string;
  scheduledDate?: string;
  doneDate?: string;
  recurrence?: string;
  tags: string[];
  text: string;
}

export interface VaultGraphNode {
  id: string;
  label: string;
  path?: string;
  type: 'note' | 'missing' | 'canvas' | 'tag' | 'skill' | 'course';
}

export interface VaultGraphEdge {
  id: string;
  from: string;
  to: string;
  label: string;
  type: 'wikilink' | 'tag' | 'canvas' | 'skill' | 'course';
}

export interface Infantecimeme {
  id: string;
  sourcePath: string;
  sourceTitle: string;
  line: number;
  kind: 'note' | 'heading' | 'task' | 'query' | 'table' | 'callout' | 'embed' | 'footnote' | 'math' | 'base' | 'canvas';
  title: string;
  text: string;
  tags: string[];
}

export interface TreehouseSkill {
  id: string;
  label: string;
  sourcePath: string;
  status: 'locked' | 'available' | 'active' | 'complete';
  dependsOn: string[];
  evidenceTasks: string[];
}

export interface TreehouseCourse {
  id: string;
  label: string;
  sourcePath: string;
  activities: string[];
  progress: number;
}

export interface VaultIndex {
  notes: IndexedVaultNote[];
  tasks: VaultDerivedTask[];
  graph: { nodes: VaultGraphNode[]; edges: VaultGraphEdge[] };
  infantecimemes: Infantecimeme[];
  skills: TreehouseSkill[];
  courses: TreehouseCourse[];
  bases: ParsedBaseFile[];
  canvases: ParsedCanvasFile[];
}

const WIKILINK_RE = /!?\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]/g;
const WIKILINK_EMBED_RE = /!\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]/g;
const MARKDOWN_IMAGE_RE = /!\[([^\]\n]*)\]\(([^)\n]+)\)/g;
const TAG_RE = /(^|[\s(])#([A-Za-z0-9_/-]+)/g;
const TASK_RE = /^(\s*)[-*]\s+\[([ xX/-])\]\s+(.*)$/;
const DONE_DATE_RE = /(?:✅|done::|\[done::)\s*(\d{4}-\d{2}-\d{2})/i;
const DUE_DATE_RE = /(?:📅|due::|\[due::)\s*(\d{4}-\d{2}-\d{2})/i;
const SCHEDULED_DATE_RE = /(?:⏳|scheduled::|\[scheduled::)\s*(\d{4}-\d{2}-\d{2})/i;
const RECURRENCE_RE = /(?:🔁|repeat::|\[repeat::)\s*([^\]\n]+)/i;
const INLINE_FIELD_RE = /\[([A-Za-z0-9_-]+)::\s*([^\]]+)\]/g;

function parseScalar(value: string): string | string[] | boolean | number {
  const trimmed = value.trim();
  if (trimmed === 'true') return true;
  if (trimmed === 'false') return false;
  if (/^-?\d+(\.\d+)?$/.test(trimmed)) return Number(trimmed);
  if (trimmed.startsWith('[') && trimmed.endsWith(']')) {
    return trimmed
      .slice(1, -1)
      .split(',')
      .map((part) => part.trim().replace(/^['"]|['"]$/g, ''))
      .filter(Boolean);
  }
  return trimmed.replace(/^['"]|['"]$/g, '');
}

function parseFrontmatter(content: string): {
  frontmatter: ParsedFrontmatter | null;
  body: string;
} {
  if (!content.startsWith('---\n')) return { frontmatter: null, body: content };
  const close = content.indexOf('\n---', 4);
  if (close < 0) return { frontmatter: null, body: content };
  const raw = content.slice(4, close).trim();
  const values: ParsedFrontmatter['values'] = {};
  for (const line of raw.split(/\r?\n/)) {
    const match = /^([A-Za-z0-9_-]+):\s*(.*)$/.exec(line);
    if (!match) continue;
    values[match[1]] = parseScalar(match[2]);
  }
  const body = content.slice(close + 4).replace(/^\r?\n/, '');
  return { frontmatter: { raw, values }, body };
}

function taskStatus(symbol: string): ParsedMarkdownTask['status'] {
  if (symbol === 'x' || symbol === 'X') return 'done';
  if (symbol === '/') return 'in-progress';
  if (symbol === '-') return 'cancelled';
  return 'pending';
}

function cleanTaskText(text: string): string {
  return text
    .replace(DONE_DATE_RE, '')
    .replace(DUE_DATE_RE, '')
    .replace(SCHEDULED_DATE_RE, '')
    .replace(RECURRENCE_RE, '')
    .replace(INLINE_FIELD_RE, '')
    .replace(/[🔺⏫🔼🔽⏬]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

function taskPriority(text: string): ParsedMarkdownTask['priority'] {
  if (/[🔺⏫]/.test(text) || /\[priority::\s*high\]/i.test(text)) return 'high';
  if (/[🔽⏬]/.test(text) || /\[priority::\s*low\]/i.test(text)) return 'low';
  return 'medium';
}

function collectInlineFields(text: string): Record<string, string> {
  const fields: Record<string, string> = {};
  for (const match of text.matchAll(INLINE_FIELD_RE)) {
    fields[match[1]] = match[2].trim();
  }
  return fields;
}

function parseFencedQueryBlocks(lines: string[]): ParsedQueryBlock[] {
  const blocks: ParsedQueryBlock[] = [];
  let open: { lang: string; line: number; body: string[] } | null = null;
  lines.forEach((line, idx) => {
    const fence = /^```([A-Za-z0-9_-]+)?\s*$/.exec(line.trim());
    if (fence && !open) {
      open = { lang: (fence[1] ?? '').toLowerCase(), line: idx + 1, body: [] };
      return;
    }
    if (line.trim() === '```' && open) {
      const kind = open.lang === 'dataview' || open.lang === 'dataviewjs'
        ? 'dataview'
        : open.lang === 'tasks'
        ? 'tasks'
        : 'unknown';
      if (kind !== 'unknown') {
        blocks.push({ lang: open.lang, line: open.line, query: open.body.join('\n'), kind });
      }
      open = null;
      return;
    }
    if (open) open.body.push(line);
  });
  return blocks;
}

function parseEmbeds(lines: string[]): ParsedEmbed[] {
  const embeds: ParsedEmbed[] = [];
  lines.forEach((line, idx) => {
    for (const match of line.matchAll(WIKILINK_EMBED_RE)) {
      embeds.push({ line: idx + 1, kind: 'wikilink', target: match[1].trim() });
    }
    for (const match of line.matchAll(MARKDOWN_IMAGE_RE)) {
      embeds.push({ line: idx + 1, kind: 'image', alt: match[1].trim(), target: match[2].trim() });
    }
  });
  return embeds;
}

function parseCallouts(lines: string[]): ParsedCallout[] {
  const callouts: ParsedCallout[] = [];
  lines.forEach((line, idx) => {
    const match = /^\s*>\s*\[!([A-Za-z0-9_-]+)\]([+-])?\s*(.*)$/.exec(line);
    if (!match) return;
    callouts.push({
      line: idx + 1,
      kind: match[1].toLowerCase(),
      fold: match[2] === '+' || match[2] === '-' ? match[2] : undefined,
      title: match[3].trim(),
    });
  });
  return callouts;
}

function parseFootnotes(lines: string[]): ParsedFootnote[] {
  return lines.flatMap((line, idx) => {
    const match = /^\s*\[\^([^\]]+)\]:\s*(.*)$/.exec(line);
    return match ? [{ line: idx + 1, id: match[1].trim(), text: match[2].trim() }] : [];
  });
}

function parseMathBlocks(lines: string[]): ParsedMathBlock[] {
  const blocks: ParsedMathBlock[] = [];
  let open: { line: number; body: string[] } | null = null;
  lines.forEach((line, idx) => {
    if (line.trim() === '$$') {
      if (open) {
        blocks.push({ line: open.line, body: open.body.join('\n') });
        open = null;
      } else {
        open = { line: idx + 1, body: [] };
      }
      return;
    }
    if (open) open.body.push(line);
  });
  return blocks;
}

function parseTables(lines: string[]): ParsedMarkdownTable[] {
  const tables: ParsedMarkdownTable[] = [];
  for (let i = 0; i < lines.length - 1; i++) {
    const header = lines[i];
    const sep = lines[i + 1];
    if (!header.includes('|') || !/^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(sep)) {
      continue;
    }
    const headers = splitTableRow(header);
    const rows: string[][] = [];
    let j = i + 2;
    while (j < lines.length && lines[j].includes('|') && lines[j].trim()) {
      rows.push(splitTableRow(lines[j]));
      j++;
    }
    tables.push({ line: i + 1, headers, rows });
    i = j;
  }
  return tables;
}

function splitTableRow(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((cell) => cell.trim());
}

function parseOutline(lines: string[]): ParsedOutlineItem[] {
  const outline: ParsedOutlineItem[] = [];
  lines.forEach((line, idx) => {
    const heading = /^(#{1,6})\s+(.+)$/.exec(line);
    if (heading) {
      outline.push({ line: idx + 1, level: heading[1].length, text: heading[2].trim(), kind: 'heading' });
      return;
    }
    const list = /^(\s*)[-*+]\s+(.+)$/.exec(line);
    if (list && !TASK_RE.test(line)) {
      outline.push({ line: idx + 1, level: Math.floor(list[1].length / 2) + 1, text: list[2].trim(), kind: 'list' });
    }
  });
  return outline;
}

export function parseNote(content: string): ParsedNote {
  const { frontmatter, body } = parseFrontmatter(content);
  const lines = body.split(/\r?\n/);
  const wikilinks = new Set<string>();
  const tags = new Set<string>();
  const tasks: ParsedMarkdownTask[] = [];
  const fencedBlocks: ParsedNote['fencedBlocks'] = [];
  const inlineFields: Record<string, string[]> = {};

  for (const match of body.matchAll(WIKILINK_RE)) {
    wikilinks.add(match[1].trim());
  }
  for (const match of body.matchAll(TAG_RE)) {
    tags.add(match[2]);
  }

  lines.forEach((line, idx) => {
    const fence = /^```([A-Za-z0-9_-]+)\s*$/.exec(line.trim());
    if (fence) fencedBlocks.push({ lang: fence[1].toLowerCase(), line: idx + 1 });
    for (const field of line.matchAll(INLINE_FIELD_RE)) {
      inlineFields[field[1]] ??= [];
      inlineFields[field[1]].push(field[2].trim());
    }
    const task = TASK_RE.exec(line);
    if (!task) return;
    const doneDate = DONE_DATE_RE.exec(task[3])?.[1];
    const dueDate = DUE_DATE_RE.exec(task[3])?.[1];
    const scheduledDate = SCHEDULED_DATE_RE.exec(task[3])?.[1];
    const recurrence = RECURRENCE_RE.exec(task[3])?.[1]?.trim();
    tasks.push({
      line: idx + 1,
      indent: task[1].length,
      statusSymbol: task[2],
      status: taskStatus(task[2]),
      text: task[3].trim(),
      cleanText: cleanTaskText(task[3]),
      doneDate,
      dueDate,
      scheduledDate,
      recurrence,
      priority: taskPriority(task[3]),
      inlineFields: collectInlineFields(task[3]),
    });
  });

  const queryBlocks = parseFencedQueryBlocks(lines);
  const unknownFenced = fencedBlocks
    .filter((block) => block.lang && !['dataview', 'dataviewjs', 'tasks', 'mermaid'].includes(block.lang))
    .map((block) => `L${block.line}: fenced '${block.lang}' preserved inert`);

  return {
    frontmatter,
    body,
    wikilinks: [...wikilinks].sort(),
    embeds: parseEmbeds(lines),
    tags: [...tags].sort(),
    tasks,
    fencedBlocks,
    dataviewBlocks: queryBlocks.filter((block) => block.kind === 'dataview'),
    taskQueryBlocks: queryBlocks.filter((block) => block.kind === 'tasks'),
    pluginWarnings: [
      ...unknownFenced,
      ...queryBlocks.map((block) => `L${block.line}: ${block.kind} query preserved inert`),
      ...(/<%[\s\S]*?%>/.test(body) ? ['Templater tags preserved inert'] : []),
    ],
    callouts: parseCallouts(lines),
    footnotes: parseFootnotes(lines),
    mathBlocks: parseMathBlocks(lines),
    tables: parseTables(lines),
    outline: parseOutline(lines),
    inlineFields,
    hasTemplater: /<%[\s\S]*?%>/.test(body),
  };
}

export function markdownForPreview(content: string): string {
  const { body } = parseFrontmatter(content);
  return body.replace(/!?\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|([^\]]+))?\]\]/g, (_m, target, alias) => {
    const label = alias || target;
    return `**${label}**`;
  });
}

export function buildVaultIndex(notes: VaultNote[]): VaultIndex {
  const indexed = notes.map((note) => {
    const parsed = parseNote(note.content);
    const title = noteTitle(note);
    const base = note.suffix === '.base' ? parseBaseFile(note) : undefined;
    const canvas = note.suffix === '.canvas' ? parseCanvasFile(note) : undefined;
    return { ...note, title, key: normalizeNoteKey(title), parsed, base, canvas };
  });

  const tasks = indexed.flatMap((note) => note.parsed.tasks.map((task) => toVaultTask(note, task)));
  const graph = buildGraph(indexed);
  const infantecimemes = buildInfantecimemes(indexed);
  const skills = buildSkills(indexed, tasks);
  const courses = buildCourses(indexed, tasks);

  return {
    notes: indexed,
    tasks,
    graph,
    infantecimemes,
    skills,
    courses,
    bases: indexed.flatMap((note) => (note.base ? [note.base] : [])),
    canvases: indexed.flatMap((note) => (note.canvas ? [note.canvas] : [])),
  };
}

function noteTitle(note: VaultNote): string {
  const parsed = parseNote(note.content);
  const frontTitle = parsed.frontmatter?.values.title;
  if (typeof frontTitle === 'string' && frontTitle.trim()) return frontTitle.trim();
  return note.name.replace(/\.[^.]+$/, '');
}

function normalizeNoteKey(value: string): string {
  return value
    .replace(/\.[^.]+$/, '')
    .trim()
    .toLowerCase()
    .replace(/\s+/g, ' ');
}

function toVaultTask(note: IndexedVaultNote, task: ParsedMarkdownTask): VaultDerivedTask {
  const tags = new Set(note.parsed.tags);
  for (const tag of task.text.matchAll(TAG_RE)) tags.add(tag[2]);
  return {
    id: `note:${note.path}:${task.line}`,
    title: task.cleanText || task.text,
    sourcePath: note.path,
    noteTitle: note.title,
    line: task.line,
    status: task.status,
    priority: task.priority,
    dueDate: task.dueDate,
    scheduledDate: task.scheduledDate,
    doneDate: task.doneDate,
    recurrence: task.recurrence,
    tags: [...tags].sort(),
    text: task.text,
  };
}

function buildGraph(notes: IndexedVaultNote[]): VaultIndex['graph'] {
  const nodes = new Map<string, VaultGraphNode>();
  const edges = new Map<string, VaultGraphEdge>();
  const byKey = new Map(notes.map((note) => [note.key, note]));

  for (const note of notes) {
    nodes.set(note.path, { id: note.path, label: note.title, path: note.path, type: 'note' });
    for (const link of note.parsed.wikilinks) {
      const target = byKey.get(normalizeNoteKey(link));
      const to = target?.path ?? `missing:${normalizeNoteKey(link)}`;
      nodes.set(to, target
        ? { id: target.path, label: target.title, path: target.path, type: 'note' }
        : { id: to, label: link, type: 'missing' });
      edges.set(`${note.path}->${to}:wikilink`, {
        id: `${note.path}->${to}:wikilink`,
        from: note.path,
        to,
        label: 'wikilink',
        type: 'wikilink',
      });
    }
    for (const tag of note.parsed.tags) {
      const tagId = `tag:${tag}`;
      nodes.set(tagId, { id: tagId, label: `#${tag}`, type: 'tag' });
      edges.set(`${note.path}->${tagId}:tag`, {
        id: `${note.path}->${tagId}:tag`,
        from: note.path,
        to: tagId,
        label: 'tag',
        type: 'tag',
      });
    }
    for (const canvas of note.canvas ? [note.canvas] : []) {
      nodes.set(note.path, { id: note.path, label: note.title, path: note.path, type: 'canvas' });
      for (const edge of canvas.edges) {
        edges.set(`${note.path}:${edge.id}`, {
          id: `${note.path}:${edge.id}`,
          from: note.path,
          to: note.path,
          label: edge.label || 'canvas edge',
          type: 'canvas',
        });
      }
    }
  }

  return { nodes: [...nodes.values()], edges: [...edges.values()] };
}

function buildInfantecimemes(notes: IndexedVaultNote[]): Infantecimeme[] {
  const blocks: Infantecimeme[] = [];
  for (const note of notes) {
    blocks.push({
      id: `note:${note.path}`,
      sourcePath: note.path,
      sourceTitle: note.title,
      line: 1,
      kind: note.suffix === '.canvas' ? 'canvas' : note.suffix === '.base' ? 'base' : 'note',
      title: note.title,
      text: firstParagraph(note.parsed.body),
      tags: note.parsed.tags,
    });
    for (const item of note.parsed.outline.filter((entry) => entry.kind === 'heading')) {
      blocks.push({
        id: `heading:${note.path}:${item.line}`,
        sourcePath: note.path,
        sourceTitle: note.title,
        line: item.line,
        kind: 'heading',
        title: item.text,
        text: sectionExcerpt(note.parsed.body, item.line),
        tags: note.parsed.tags,
      });
    }
    for (const task of note.parsed.tasks) {
      blocks.push({
        id: `task:${note.path}:${task.line}`,
        sourcePath: note.path,
        sourceTitle: note.title,
        line: task.line,
        kind: 'task',
        title: task.cleanText || task.text,
        text: task.text,
        tags: note.parsed.tags,
      });
    }
    for (const query of [...note.parsed.dataviewBlocks, ...note.parsed.taskQueryBlocks]) {
      blocks.push({
        id: `query:${note.path}:${query.line}`,
        sourcePath: note.path,
        sourceTitle: note.title,
        line: query.line,
        kind: 'query',
        title: `${query.kind}: ${query.query.split(/\r?\n/)[0] ?? ''}`.trim(),
        text: query.query,
        tags: note.parsed.tags,
      });
    }
    for (const table of note.parsed.tables) {
      blocks.push({
        id: `table:${note.path}:${table.line}`,
        sourcePath: note.path,
        sourceTitle: note.title,
        line: table.line,
        kind: 'table',
        title: table.headers.join(' / '),
        text: `${table.rows.length} rows`,
        tags: note.parsed.tags,
      });
    }
    for (const callout of note.parsed.callouts) {
      blocks.push({
        id: `callout:${note.path}:${callout.line}`,
        sourcePath: note.path,
        sourceTitle: note.title,
        line: callout.line,
        kind: 'callout',
        title: callout.title || callout.kind,
        text: sectionExcerpt(note.parsed.body, callout.line),
        tags: note.parsed.tags,
      });
    }
    for (const embed of note.parsed.embeds) {
      blocks.push({
        id: `embed:${note.path}:${embed.line}:${embed.target}`,
        sourcePath: note.path,
        sourceTitle: note.title,
        line: embed.line,
        kind: 'embed',
        title: embed.alt || embed.target,
        text: embed.kind === 'image' ? `image: ${embed.target}` : `embed: ${embed.target}`,
        tags: note.parsed.tags,
      });
    }
    for (const footnote of note.parsed.footnotes) {
      blocks.push({
        id: `footnote:${note.path}:${footnote.line}:${footnote.id}`,
        sourcePath: note.path,
        sourceTitle: note.title,
        line: footnote.line,
        kind: 'footnote',
        title: `^${footnote.id}`,
        text: footnote.text,
        tags: note.parsed.tags,
      });
    }
    for (const math of note.parsed.mathBlocks) {
      blocks.push({
        id: `math:${note.path}:${math.line}`,
        sourcePath: note.path,
        sourceTitle: note.title,
        line: math.line,
        kind: 'math',
        title: 'Math block',
        text: math.body,
        tags: note.parsed.tags,
      });
    }
  }
  return blocks;
}

function firstParagraph(body: string): string {
  return body
    .split(/\n\s*\n/)
    .map((part) => part.replace(/^#+\s+.+$/gm, '').trim())
    .find(Boolean)
    ?.slice(0, 280) ?? '';
}

function sectionExcerpt(body: string, line: number): string {
  return body
    .split(/\r?\n/)
    .slice(line, line + 5)
    .join('\n')
    .trim()
    .slice(0, 320);
}

function buildSkills(notes: IndexedVaultNote[], tasks: VaultDerivedTask[]): TreehouseSkill[] {
  const out: TreehouseSkill[] = [];
  for (const note of notes) {
    const skillValue = note.parsed.frontmatter?.values.skill ?? note.parsed.frontmatter?.values.skills;
    const tags = note.parsed.tags;
    const names = [
      ...valueList(skillValue),
      ...tags.filter((tag) => tag.startsWith('skill/')).map((tag) => tag.slice('skill/'.length)),
    ];
    for (const name of names) {
      const id = normalizeNoteKey(name);
      const evidenceTasks = tasks
        .filter((task) => task.sourcePath === note.path && task.status === 'done')
        .map((task) => task.id);
      out.push({
        id,
        label: name,
        sourcePath: note.path,
        status: evidenceTasks.length > 0 ? 'complete' : 'active',
        dependsOn: valueList(note.parsed.frontmatter?.values.depends_on ?? note.parsed.frontmatter?.values.prerequisite),
        evidenceTasks,
      });
    }
  }
  return dedupeBy(out, (skill) => skill.id);
}

function buildCourses(notes: IndexedVaultNote[], tasks: VaultDerivedTask[]): TreehouseCourse[] {
  const out: TreehouseCourse[] = [];
  for (const note of notes) {
    const courseValue = note.parsed.frontmatter?.values.course ?? note.parsed.frontmatter?.values.courses;
    const tags = note.parsed.tags;
    const names = [
      ...valueList(courseValue),
      ...tags.filter((tag) => tag.startsWith('course/')).map((tag) => tag.slice('course/'.length)),
    ];
    for (const name of names) {
      const noteTasks = tasks.filter((task) => task.sourcePath === note.path);
      const complete = noteTasks.filter((task) => task.status === 'done').length;
      out.push({
        id: normalizeNoteKey(name),
        label: name,
        sourcePath: note.path,
        activities: note.parsed.outline.map((item) => item.text),
        progress: noteTasks.length ? Math.round((complete / noteTasks.length) * 100) : 0,
      });
    }
  }
  return dedupeBy(out, (course) => course.id);
}

function valueList(value: unknown): string[] {
  if (Array.isArray(value)) return value.map(String).filter(Boolean);
  if (typeof value === 'string' && value.trim()) return [value.trim()];
  return [];
}

function dedupeBy<T>(items: T[], key: (item: T) => string): T[] {
  const seen = new Set<string>();
  const out: T[] = [];
  for (const item of items) {
    const id = key(item);
    if (seen.has(id)) continue;
    seen.add(id);
    out.push(item);
  }
  return out;
}

function parseBaseFile(note: VaultNote): ParsedBaseFile {
  const values: Record<string, unknown> = {};
  const filters: string[] = [];
  const columns: string[] = [];
  const unknownLines: string[] = [];
  let current: 'filters' | 'columns' | null = null;

  for (const raw of note.content.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line === '---') continue;
    const key = /^([A-Za-z0-9_-]+):\s*(.*)$/.exec(line);
    if (key) {
      current = key[1] === 'filters' ? 'filters' : key[1] === 'columns' || key[1] === 'properties' ? 'columns' : null;
      values[key[1]] = key[2] ? parseScalar(key[2]) : [];
      continue;
    }
    const list = /^-\s+(.+)$/.exec(line);
    if (list && current === 'filters') filters.push(list[1]);
    else if (list && current === 'columns') columns.push(list[1].replace(/^name:\s*/, ''));
    else unknownLines.push(raw);
  }

  return { sourcePath: note.path, keys: Object.keys(values), values, filters, columns, unknownLines };
}

function parseCanvasFile(note: VaultNote): ParsedCanvasFile {
  try {
    const raw = JSON.parse(note.content) as {
      nodes?: { id?: string; type?: string; text?: string; file?: string }[];
      edges?: { id?: string; fromNode?: string; toNode?: string; label?: string }[];
    };
    return {
      sourcePath: note.path,
      nodes: (raw.nodes ?? []).map((node, idx) => ({
        id: node.id ?? `node-${idx}`,
        type: node.type,
        label: node.text ?? node.file ?? node.id ?? `node ${idx + 1}`,
        file: node.file,
      })),
      edges: (raw.edges ?? []).map((edge, idx) => ({
        id: edge.id ?? `edge-${idx}`,
        from: edge.fromNode ?? '',
        to: edge.toNode ?? '',
        label: edge.label,
      })),
    };
  } catch {
    return { sourcePath: note.path, nodes: [], edges: [] };
  }
}
