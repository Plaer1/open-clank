'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import ReactMarkdown from 'react-markdown';
import {
  Bold,
  BookOpen,
  Braces,
  Clipboard,
  Code,
  Command as CommandIcon,
  Download,
  Eye,
  FileSearch,
  FileCode2,
  FileText,
  Hash,
  Heading1,
  Heading2,
  Italic,
  Keyboard,
  Link2,
  List,
  ListChecks,
  ListOrdered,
  PanelLeft,
  PanelRight,
  Pencil,
  Plus,
  Quote,
  RefreshCw,
  Save,
  Search,
  Sparkles,
  SquareCheck,
  Settings2,
  Table2,
  Tags,
  Trash2,
  Wand,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { parseNote, markdownForPreview, type VaultNote, type VaultNoteEntry } from '@/lib/notes';
import { readNoteJumpFromHash } from '@/lib/noteNavigation';
import { LiveMarkdownEditor, type LiveMarkdownEditorHandle } from '@/components/editor/LiveMarkdownEditor';

interface NotesResponse {
  vaultPath: string;
  notes: VaultNoteEntry[];
}

interface BacklinkResponse {
  backlinks: { sourcePath: string; sourceTitle: string; type: string; line?: number }[];
}

interface SearchResponse {
  results: (VaultNoteEntry & { excerpt: string })[];
}

interface UiStateResponse {
  notesEditorMode?: EditorMode;
  lastActiveNote?: string;
}

type EditorMode = 'live' | 'source' | 'reading';
type SelectionMode = 'select' | 'cursor';
type SidePane = 'properties' | 'outline' | 'links' | 'tasks' | 'queries';

interface EditorCommandItem {
  id: string;
  label: string;
  group: string;
  shortcut?: string;
  icon: ReactNode;
}

const EDITOR_COMMANDS: EditorCommandItem[] = [
  { id: 'bold', label: 'Bold', group: 'Format', shortcut: 'Ctrl+B', icon: <Bold className="h-3.5 w-3.5" /> },
  { id: 'italic', label: 'Italic', group: 'Format', shortcut: 'Ctrl+I', icon: <Italic className="h-3.5 w-3.5" /> },
  { id: 'inline-code', label: 'Inline code', group: 'Format', icon: <Code className="h-3.5 w-3.5" /> },
  { id: 'code-block', label: 'Code block', group: 'Insert', icon: <FileCode2 className="h-3.5 w-3.5" /> },
  { id: 'h1', label: 'Heading 1', group: 'Structure', icon: <Heading1 className="h-3.5 w-3.5" /> },
  { id: 'h2', label: 'Heading 2', group: 'Structure', icon: <Heading2 className="h-3.5 w-3.5" /> },
  { id: 'h3', label: 'Heading 3', group: 'Structure', icon: <Hash className="h-3.5 w-3.5" /> },
  { id: 'quote', label: 'Blockquote', group: 'Structure', icon: <Quote className="h-3.5 w-3.5" /> },
  { id: 'bullet', label: 'Bullet list', group: 'Structure', shortcut: 'Ctrl+Shift+8', icon: <List className="h-3.5 w-3.5" /> },
  { id: 'numbered', label: 'Numbered list', group: 'Structure', shortcut: 'Ctrl+Shift+7', icon: <ListOrdered className="h-3.5 w-3.5" /> },
  { id: 'task', label: 'Task checkbox', group: 'Tasks', shortcut: 'Ctrl+Enter', icon: <SquareCheck className="h-3.5 w-3.5" /> },
  { id: 'due', label: 'Due today marker', group: 'Tasks', icon: <ListChecks className="h-3.5 w-3.5" /> },
  { id: 'scheduled', label: 'Scheduled today marker', group: 'Tasks', icon: <ListChecks className="h-3.5 w-3.5" /> },
  { id: 'done-marker', label: 'Done today marker', group: 'Tasks', icon: <ListChecks className="h-3.5 w-3.5" /> },
  { id: 'priority-high', label: 'High priority marker', group: 'Tasks', icon: <ListChecks className="h-3.5 w-3.5" /> },
  { id: 'repeat-weekly', label: 'Weekly recurrence marker', group: 'Tasks', icon: <ListChecks className="h-3.5 w-3.5" /> },
  { id: 'wikilink', label: 'Wikilink', group: 'Links', shortcut: 'Ctrl+K', icon: <Link2 className="h-3.5 w-3.5" /> },
  { id: 'tag', label: 'Tag', group: 'Links', icon: <Tags className="h-3.5 w-3.5" /> },
  { id: 'table', label: 'Markdown table', group: 'Tables', icon: <Table2 className="h-3.5 w-3.5" /> },
  { id: 'table-row', label: 'Append table row', group: 'Tables', icon: <Table2 className="h-3.5 w-3.5" /> },
  { id: 'table-column', label: 'Append table column', group: 'Tables', icon: <Table2 className="h-3.5 w-3.5" /> },
  { id: 'table-normalize', label: 'Normalize table', group: 'Tables', icon: <Table2 className="h-3.5 w-3.5" /> },
  { id: 'callout', label: 'Obsidian callout', group: 'Insert', icon: <Quote className="h-3.5 w-3.5" /> },
  { id: 'frontmatter', label: 'Frontmatter', group: 'Insert', icon: <Braces className="h-3.5 w-3.5" /> },
  { id: 'dataview', label: 'Dataview block', group: 'Plugin syntax', icon: <Braces className="h-3.5 w-3.5" /> },
  { id: 'tasks-block', label: 'Tasks query block', group: 'Plugin syntax', icon: <ListChecks className="h-3.5 w-3.5" /> },
  { id: 'templater', label: 'Templater tag', group: 'Plugin syntax', icon: <Sparkles className="h-3.5 w-3.5" /> },
  { id: 'mermaid', label: 'Mermaid mind map', group: 'Mind map', icon: <FileCode2 className="h-3.5 w-3.5" /> },
  { id: 'base', label: 'Base file starter', group: 'Bases', icon: <Braces className="h-3.5 w-3.5" /> },
  { id: 'canvas', label: 'Canvas starter', group: 'Canvas', icon: <FileCode2 className="h-3.5 w-3.5" /> },
  { id: 'copy-link', label: 'Copy line link', group: 'Navigation', icon: <Clipboard className="h-3.5 w-3.5" /> },
];

async function fetchNotes(): Promise<NotesResponse> {
  const res = await fetch('/api/notes');
  if (!res.ok) throw new Error(`notes request failed: ${res.status}`);
  return res.json();
}

async function fetchNote(path: string): Promise<VaultNote> {
  const res = await fetch(`/api/note?path=${encodeURIComponent(path)}`);
  if (!res.ok) throw new Error(`note request failed: ${res.status}`);
  return res.json();
}

async function saveNote(path: string, content: string): Promise<{ backup?: string }> {
  const res = await fetch('/api/note', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path, content }),
  });
  if (!res.ok) throw new Error(`save failed: ${res.status}`);
  return res.json();
}

async function deleteNote(path: string) {
  const res = await fetch('/api/note/delete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  });
  if (!res.ok) throw new Error(`delete failed: ${res.status}`);
  return res.json();
}

async function renameNote(path: string, newPath: string) {
  const res = await fetch('/api/note/rename', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path, newPath }),
  });
  if (!res.ok) throw new Error(`rename failed: ${res.status}`);
  return res.json();
}

async function fetchBacklinks(path: string): Promise<BacklinkResponse> {
  const res = await fetch(`/api/backlinks?path=${encodeURIComponent(path)}`);
  if (!res.ok) throw new Error(`backlinks request failed: ${res.status}`);
  return res.json();
}

async function fetchSearch(query: string): Promise<SearchResponse> {
  const res = await fetch(`/api/search?q=${encodeURIComponent(query)}`);
  if (!res.ok) throw new Error(`search failed: ${res.status}`);
  return res.json();
}

function safeEditorMode(value: unknown): EditorMode {
  if (value === 'source' || value === 'live' || value === 'reading') return value;
  if (value === 'preview') return 'reading';
  return 'live';
}

function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

function normalizeNewPath(path: string): string {
  const clean = path.trim().replace(/^\/+/, '') || 'Untitled.md';
  return /\.[A-Za-z0-9]+$/.test(clean) ? clean : `${clean}.md`;
}

function noteSuggestionLabel(path: string): string {
  return path.replace(/\.[^.]+$/, '').split('/').pop() || path;
}

function getLineBounds(text: string, position: number) {
  const start = text.lastIndexOf('\n', Math.max(0, position - 1)) + 1;
  const next = text.indexOf('\n', position);
  const end = next === -1 ? text.length : next;
  return { start, end };
}

function splitTableRow(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((cell) => cell.trim());
}

function makeTableRow(cells: string[]): string {
  return `| ${cells.join(' | ')} |`;
}

function findTableRange(lines: string[], lineIndex: number) {
  if (!lines[lineIndex]?.includes('|')) return null;
  let start = lineIndex;
  let end = lineIndex;
  while (start > 0 && lines[start - 1].includes('|') && lines[start - 1].trim()) start--;
  while (end < lines.length - 1 && lines[end + 1].includes('|') && lines[end + 1].trim()) end++;
  if (end <= start) return null;
  return { start, end };
}

function countWords(text: string): number {
  return text.trim() ? text.trim().split(/\s+/).length : 0;
}

export function NotesView() {
  const [vaultPath, setVaultPath] = useState('');
  const [notes, setNotes] = useState<VaultNoteEntry[]>([]);
  const [activePath, setActivePath] = useState('');
  const [activeNote, setActiveNote] = useState<VaultNote | null>(null);
  const [draft, setDraft] = useState('');
  const [query, setQuery] = useState('');
  const [newPath, setNewPath] = useState('Untitled.md');
  const [renamePath, setRenamePath] = useState('');
  const [targetLine, setTargetLine] = useState<number | null>(null);
  const [backlinks, setBacklinks] = useState<BacklinkResponse['backlinks']>([]);
  const [searchResults, setSearchResults] = useState<SearchResponse['results']>([]);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [editorMode, setEditorMode] = useState<EditorMode>('live');
  const [commandOpen, setCommandOpen] = useState(false);
  const [commandPoint, setCommandPoint] = useState<{ left: number; top: number } | null>(null);
  const [commandQuery, setCommandQuery] = useState('');
  const [cursorLine, setCursorLine] = useState(1);
  const [lastSaveBackup, setLastSaveBackup] = useState('');
  const [sidePane, setSidePane] = useState<SidePane>('outline');
  const editorRef = useRef<LiveMarkdownEditorHandle | null>(null);

  const dirty = activeNote !== null && draft !== activeNote.content;

  async function refresh(nextPath = activePath) {
    setError('');
    try {
      const payload = await fetchNotes();
      setVaultPath(payload.vaultPath);
      setNotes(payload.notes);
      const path = nextPath || payload.notes[0]?.path || '';
      setActivePath(path);
      if (path) {
        const note = await fetchNote(path);
        setActiveNote(note);
        setDraft(note.content);
        setRenamePath(note.path);
      } else {
        setActiveNote(null);
        setDraft('');
        setRenamePath('');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'notes request failed');
    }
  }

  async function loadNote(path: string, line?: number) {
    if (dirty && !window.confirm('Discard unsaved changes?')) return;
    setActivePath(path);
    setTargetLine(line ?? null);
  }

  async function createFromPath() {
    const path = normalizeNewPath(newPath);
    const title = noteSuggestionLabel(path);
    const content = `---\ntitle: ${title}\n---\n\n# ${title}\n\n`;
    await saveNote(path, content);
    setNotice(`Created ${path}`);
    await refresh(path);
  }

  async function persistDraft() {
    if (!activePath) return;
    const result = await saveNote(activePath, draft);
    setLastSaveBackup(result.backup ?? '');
    setNotice(result.backup ? `Saved ${activePath}; backup ${result.backup}` : `Saved ${activePath}`);
    const note = await fetchNote(activePath);
    setActiveNote(note);
    setDraft(note.content);
    await refresh(activePath);
  }

  async function renameActive() {
    if (!activePath) return;
    const path = normalizeNewPath(renamePath);
    if (path === activePath) return;
    await renameNote(activePath, path);
    setNotice(`Renamed ${activePath} -> ${path}`);
    await refresh(path);
  }

  async function removeActive() {
    if (!activePath) return;
    if (!window.confirm(`Back up and delete ${activePath}?`)) return;
    await deleteNote(activePath);
    setNotice(`Deleted ${activePath}`);
    await refresh('');
  }

  async function downloadExport(path: string, filename: string) {
    const res = await fetch(path);
    if (!res.ok) {
      setError(`export failed: ${res.status}`);
      return;
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }

  async function runSearch() {
    if (!query.trim()) {
      setSearchResults([]);
      return;
    }
    const payload = await fetchSearch(query);
    setSearchResults(payload.results);
  }

  function setDraftWithSelection(next: string, start: number, end = start, mode: SelectionMode = 'cursor') {
    setDraft(next);
    requestAnimationFrame(() => {
      const editor = editorRef.current;
      if (!editor) return;
      editor.focus();
      editor.setSelectionRange(start, mode === 'select' ? end : start);
      setCursorLine(next.slice(0, start).split(/\r?\n/).length);
    });
  }

  function replaceSelection(text: string, mode: SelectionMode = 'cursor') {
    const selection = editorRef.current?.getSelection() ?? { start: draft.length, end: draft.length };
    const start = selection.start;
    const end = selection.end;
    const next = `${draft.slice(0, start)}${text}${draft.slice(end)}`;
    setDraftWithSelection(next, start + text.length, start + text.length, mode);
  }

  function wrapSelection(before: string, after = before, placeholder = 'text') {
    const selection = editorRef.current?.getSelection() ?? { start: draft.length, end: draft.length };
    const start = selection.start;
    const end = selection.end;
    const selected = draft.slice(start, end) || placeholder;
    const inserted = `${before}${selected}${after}`;
    const next = `${draft.slice(0, start)}${inserted}${draft.slice(end)}`;
    setDraftWithSelection(next, start + before.length, start + before.length + selected.length, 'select');
  }

  function transformSelectedLines(transform: (line: string, index: number) => string) {
    const selection = editorRef.current?.getSelection() ?? { start: draft.length, end: draft.length };
    const selectionStart = selection.start;
    const selectionEnd = selection.end;
    const lineStart = draft.lastIndexOf('\n', Math.max(0, selectionStart - 1)) + 1;
    const nextBreak = draft.indexOf('\n', selectionEnd);
    const lineEnd = nextBreak === -1 ? draft.length : nextBreak;
    const block = draft.slice(lineStart, lineEnd);
    const transformed = block.split('\n').map(transform).join('\n');
    const next = `${draft.slice(0, lineStart)}${transformed}${draft.slice(lineEnd)}`;
    setDraftWithSelection(next, lineStart, lineStart + transformed.length, 'select');
  }

  function setHeading(level: 1 | 2 | 3) {
    transformSelectedLines((line) => `${'#'.repeat(level)} ${line.replace(/^#{1,6}\s+/, '')}`);
  }

  function prefixLines(prefix: string) {
    transformSelectedLines((line) => (line.trim() ? `${prefix}${line.replace(/^(\s*)([-*+]\s+|\d+\.\s+|>\s+)?/, '$1')}` : line));
  }

  function numberLines() {
    transformSelectedLines((line, index) => (line.trim() ? `${index + 1}. ${line.replace(/^(\s*)([-*+]\s+|\d+\.\s+)?/, '$1')}` : line));
  }

  function toggleTaskAtCursor() {
    const position = editorRef.current?.getCursor() ?? draft.length;
    const bounds = getLineBounds(draft, position);
    const line = draft.slice(bounds.start, bounds.end);
    let nextLine = line;
    if (/^(\s*)[-*]\s+\[ \]\s+/.test(line)) {
      nextLine = line.replace(/^(\s*)[-*]\s+\[ \]\s+/, '$1- [x] ');
    } else if (/^(\s*)[-*]\s+\[[xX]\]\s+/.test(line)) {
      nextLine = line.replace(/^(\s*)[-*]\s+\[[xX]\]\s+/, '$1- [ ] ');
    } else {
      nextLine = line.trim() ? `- [ ] ${line.trim()}` : '- [ ] ';
    }
    const next = `${draft.slice(0, bounds.start)}${nextLine}${draft.slice(bounds.end)}`;
    setDraftWithSelection(next, bounds.start + nextLine.length);
  }

  function insertFence(lang: string, body = '') {
    replaceSelection(`\n\`\`\`${lang}\n${body}\n\`\`\`\n`);
  }

  function insertTable() {
    replaceSelection('\n| Column A | Column B |\n| --- | --- |\n|  |  |\n');
  }

  function updateCurrentTable(kind: 'row' | 'column' | 'normalize') {
    const position = editorRef.current?.getCursor() ?? draft.length;
    const lines = draft.split('\n');
    const lineIndex = draft.slice(0, position).split('\n').length - 1;
    const range = findTableRange(lines, lineIndex);
    if (!range) {
      insertTable();
      return;
    }

    const table = lines.slice(range.start, range.end + 1).map(splitTableRow);
    const width = Math.max(...table.map((row) => row.length));
    const normalized = table.map((row) => [...row, ...Array.from({ length: width - row.length }, () => '')]);

    if (kind === 'row') {
      normalized.push(Array.from({ length: width }, () => ''));
    }
    if (kind === 'column') {
      normalized.forEach((row, idx) => row.push(idx === 1 ? '---' : ''));
    }

    const nextRows = normalized.map(makeTableRow);
    const nextLines = [...lines.slice(0, range.start), ...nextRows, ...lines.slice(range.end + 1)];
    const before = lines.slice(0, range.start).join('\n');
    const start = before.length + (range.start > 0 ? 1 : 0);
    setDraftWithSelection(nextLines.join('\n'), start, start + nextRows.join('\n').length, 'select');
  }

  function insertFrontmatter() {
    if (draft.startsWith('---\n')) {
      setNotice('Frontmatter already exists.');
      return;
    }
    const title = activeNote ? activeNote.name.replace(/\.[^.]+$/, '') : 'Untitled';
    const next = `---\ntitle: ${title}\ntags: []\n---\n\n${draft}`;
    setDraftWithSelection(next, 4, 4);
  }

  function insertCallout() {
    replaceSelection('\n> [!note]\n> Callout text\n');
  }

  function insertWikilink(target?: string) {
    const selection = editorRef.current?.getSelection() ?? { start: draft.length, end: draft.length };
    const start = selection.start;
    const end = selection.end;
    const selected = draft.slice(start, end).trim();
    const label = target ?? selected ?? 'Note Title';
    replaceSelection(`[[${label}]]`);
  }

  function insertTag(tag?: string) {
    replaceSelection(tag ? `#${tag}` : '#tag');
  }

  function copyLinkToLine(line = cursorLine) {
    if (!activePath) return;
    const value = `${window.location.origin}${window.location.pathname}#note=${encodeURIComponent(activePath)}&line=${line}`;
    navigator.clipboard?.writeText(value).catch(() => undefined);
    setNotice(`Copied link to ${activePath}:L${line}`);
  }

  function focusLine(line: number) {
    setTargetLine(line);
    requestAnimationFrame(() => {
      const result = editorRef.current?.focusLine(line);
      if (result) setCursorLine(result.line);
    });
  }

  function insertSlashCommand() {
    replaceSelection('/');
    openCommandPalette(true);
    setCommandQuery('');
  }

  function openCommandPalette(forceOpen = false) {
    const rect = editorRef.current?.getCursorRect();
    if (rect) {
      setCommandPoint({
        left: Math.max(8, Math.min(window.innerWidth - 360, rect.left)),
        top: Math.max(56, Math.min(window.innerHeight - 320, rect.bottom + 8)),
      });
    } else {
      setCommandPoint(null);
    }
    setCommandOpen((open) => (forceOpen ? true : !open));
  }

  useEffect(() => {
    let cancelled = false;
    async function init() {
      try {
        const res = await fetch('/api/ui-state');
        const state = (res.ok ? await res.json() : {}) as UiStateResponse;
        if (cancelled) return;
        setEditorMode(safeEditorMode(state.notesEditorMode));
        const jump = readNoteJumpFromHash(window.location.hash);
        if (jump) {
          setTargetLine(jump.line ?? null);
          await refresh(jump.path);
        } else {
          await refresh(state.lastActiveNote);
        }
      } catch {
        if (!cancelled) await refresh();
      }
    }
    void init();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    function syncJump() {
      const jump = readNoteJumpFromHash(window.location.hash);
      if (!jump) return;
      setActivePath(jump.path);
      setTargetLine(jump.line ?? null);
    }
    syncJump();
    window.addEventListener('hashchange', syncJump);
    return () => window.removeEventListener('hashchange', syncJump);
  }, []);

  useEffect(() => {
    if (!activePath) return;
    fetchNote(activePath)
      .then((note) => {
        setActiveNote(note);
        setDraft(note.content);
        setRenamePath(note.path);
      })
      .catch((err) => setError(err instanceof Error ? err.message : 'note request failed'));
    fetchBacklinks(activePath)
      .then((payload) => setBacklinks(Array.isArray(payload.backlinks) ? payload.backlinks : []))
      .catch(() => setBacklinks([]));
  }, [activePath]);

  useEffect(() => {
    if (!activeNote || !targetLine || targetLine < 1) return;
    focusLine(targetLine);
    setNotice(`Jumped to ${activeNote.path}:L${targetLine}`);
  }, [activeNote, draft, targetLine]);

  useEffect(() => {
    if (!activePath) return;
    const payload: UiStateResponse = { notesEditorMode: editorMode };
    if (activePath) payload.lastActiveNote = activePath;
    fetch('/api/ui-state', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }).catch(() => undefined);
  }, [activePath, editorMode]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return notes;
    return notes.filter((note) => note.path.toLowerCase().includes(q));
  }, [notes, query]);

  const parsed = useMemo(() => (draft ? parseNote(draft) : null), [draft]);
  const previewMarkdown = useMemo(() => (draft ? markdownForPreview(draft) : ''), [draft]);
  const currentLineText = useMemo(() => {
    const lines = draft.split(/\r?\n/);
    return lines[Math.max(0, cursorLine - 1)] ?? '';
  }, [cursorLine, draft]);
  const stats = useMemo(
    () => ({
      lines: draft ? draft.split(/\r?\n/).length : 0,
      words: countWords(draft),
      tasks: parsed?.tasks.length ?? 0,
      done: parsed?.tasks.filter((task) => task.status === 'done').length ?? 0,
    }),
    [draft, parsed],
  );

  const noteSuggestions = useMemo(() => notes.slice(0, 24), [notes]);
  const tagSuggestions = parsed?.tags.slice(0, 20) ?? [];
  const sidePanes: { id: SidePane; label: string; count: number; icon: ReactNode }[] = [
    { id: 'properties', label: 'Properties', count: parsed?.frontmatter ? Object.keys(parsed.frontmatter.values).length : 0, icon: <Settings2 className="h-3 w-3" /> },
    { id: 'outline', label: 'Outline', count: parsed?.outline.length ?? 0, icon: <BookOpen className="h-3 w-3" /> },
    { id: 'links', label: 'Links', count: (parsed?.wikilinks.length ?? 0) + backlinks.length, icon: <Link2 className="h-3 w-3" /> },
    { id: 'tasks', label: 'Tasks', count: parsed?.tasks.length ?? 0, icon: <ListChecks className="h-3 w-3" /> },
    { id: 'queries', label: 'Queries', count: (parsed?.tables.length ?? 0) + (parsed?.dataviewBlocks.length ?? 0) + (parsed?.taskQueryBlocks.length ?? 0), icon: <FileSearch className="h-3 w-3" /> },
  ];

  function runEditorCommand(id: string) {
    if (id === 'save') void persistDraft();
    else if (id === 'palette' || id === 'quick-open') openCommandPalette();
    else if (id === 'bold') wrapSelection('**');
    else if (id === 'italic') wrapSelection('_');
    else if (id === 'inline-code') wrapSelection('`');
    else if (id === 'code-block') insertFence('text', 'code');
    else if (id === 'h1') setHeading(1);
    else if (id === 'h2') setHeading(2);
    else if (id === 'h3') setHeading(3);
    else if (id === 'quote') prefixLines('> ');
    else if (id === 'bullet') prefixLines('- ');
    else if (id === 'numbered') numberLines();
    else if (id === 'task') toggleTaskAtCursor();
    else if (id === 'due') replaceSelection(` [due:: ${todayIso()}]`);
    else if (id === 'scheduled') replaceSelection(` [scheduled:: ${todayIso()}]`);
    else if (id === 'done-marker') replaceSelection(` [done:: ${todayIso()}]`);
    else if (id === 'priority-high') replaceSelection(' [priority:: high]');
    else if (id === 'repeat-weekly') replaceSelection(' [repeat:: every week]');
    else if (id === 'wikilink') insertWikilink();
    else if (id === 'tag') insertTag();
    else if (id === 'table') insertTable();
    else if (id === 'table-row') updateCurrentTable('row');
    else if (id === 'table-column') updateCurrentTable('column');
    else if (id === 'table-normalize') updateCurrentTable('normalize');
    else if (id === 'callout') insertCallout();
    else if (id === 'frontmatter') insertFrontmatter();
    else if (id === 'dataview') insertFence('dataview', 'LIST FROM ""');
    else if (id === 'tasks-block') insertFence('tasks', 'not done');
    else if (id === 'templater') replaceSelection('<% tp.date.now("YYYY-MM-DD") %>');
    else if (id === 'mermaid') insertFence('mermaid', 'mindmap\n  root((Idea))\n    Branch');
    else if (id === 'base') replaceSelection('source: "vault"\nfilters:\n  - "tag contains project"\ncolumns:\n  - title\n  - status\n');
    else if (id === 'canvas') replaceSelection('{"nodes":[],"edges":[]}');
    else if (id === 'copy-link') copyLinkToLine(cursorLine);
  }

  const visibleCommands = EDITOR_COMMANDS.filter((command) => {
    const q = commandQuery.trim().toLowerCase();
    if (!q) return true;
    return `${command.label} ${command.group} ${command.shortcut ?? ''}`.toLowerCase().includes(q);
  });

  const editorPane = (
    <div className="min-h-0 border-b xl:border-b-0 border-slate-800">
      <LiveMarkdownEditor
        ref={editorRef}
        value={draft}
        mode={editorMode === 'source' ? 'source' : 'live'}
        onChange={setDraft}
        onCursorLineChange={setCursorLine}
        onCommand={runEditorCommand}
      />
    </div>
  );

  const previewPane = (
    <div className="copal-preview">
      {activeNote?.suffix === '.md' || activeNote?.suffix === '.markdown' ? (
        <article className="prose prose-invert prose-slate max-w-none prose-headings:scroll-mt-20 prose-a:text-cyan-300 prose-code:text-cyan-200 prose-pre:bg-slate-900 prose-pre:border prose-pre:border-slate-700">
          <ReactMarkdown>{previewMarkdown}</ReactMarkdown>
        </article>
      ) : (
        <pre className="text-xs text-slate-200 bg-slate-900/80 border border-slate-700 rounded-md p-3 overflow-x-auto whitespace-pre-wrap">{draft}</pre>
      )}
    </div>
  );

  return (
    <div className="copal-workspace w-full h-full grid grid-cols-1 xl:grid-cols-[44px_280px_minmax(0,1fr)_330px]">
      <nav className="copal-ribbon" aria-label="Notes workspace tools">
        <Button size="icon" variant="outline" className="copal-ribbon-button" onClick={() => refresh()} title="Refresh vault">
          <RefreshCw className="h-3.5 w-3.5" />
        </Button>
        <Button size="icon" variant="outline" className="copal-ribbon-button" onClick={createFromPath} title="Create note">
          <Plus className="h-3.5 w-3.5" />
        </Button>
        <Button size="icon" variant="outline" className="copal-ribbon-button" onClick={runSearch} title="Search contents">
          <Search className="h-3.5 w-3.5" />
        </Button>
        <Button size="icon" variant="outline" className="copal-ribbon-button" onClick={() => openCommandPalette()} title="Command palette">
          <CommandIcon className="h-3.5 w-3.5" />
        </Button>
        <Button size="icon" variant="outline" className="copal-ribbon-button" onClick={() => copyLinkToLine()} disabled={!activePath} title="Copy line link">
          <Clipboard className="h-3.5 w-3.5" />
        </Button>
        <div className="mt-auto flex flex-col items-center gap-1 text-[10px] text-slate-600">
          <span>{notes.length}</span>
          <FileText className="h-3.5 w-3.5" />
        </div>
      </nav>

      <aside className="copal-pane border-b xl:border-b-0 xl:border-r border-slate-800">
        <div className="copal-pane-header space-y-2">
          <div className="flex items-center justify-between gap-2">
            <div className="min-w-0">
              <div className="text-xs font-semibold text-slate-200">Vault files</div>
              <div className="text-[10px] text-slate-500 truncate" title={vaultPath}>
                {vaultPath || 'loading...'}
              </div>
            </div>
            <Button size="icon" variant="outline" className="h-7 w-7 border-slate-700 bg-slate-900 xl:hidden" onClick={() => refresh()} title="Refresh notes">
              <RefreshCw className="h-3.5 w-3.5" />
            </Button>
          </div>
          <div className="flex gap-1">
            <Input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Filter/search..." className="h-8 bg-slate-900 border-slate-700 text-xs" />
            <Button size="icon" variant="outline" className="h-8 w-8 border-slate-700 bg-slate-900" onClick={runSearch} title="Search contents">
              <Search className="h-3.5 w-3.5" />
            </Button>
          </div>
          <div className="flex gap-1">
            <Input value={newPath} onChange={(event) => setNewPath(event.target.value)} className="h-8 bg-slate-900 border-slate-700 text-xs" />
            <Button size="icon" variant="outline" className="h-8 w-8 border-slate-700 bg-slate-900" onClick={createFromPath} title="Create note">
              <Plus className="h-3.5 w-3.5" />
            </Button>
          </div>
        </div>

        <div className="flex-1 min-h-0 overflow-y-auto">
          {filtered.map((note) => (
            <button
              key={note.path}
              onClick={() => loadNote(note.path)}
              className={`w-full text-left px-3 py-2 border-b border-slate-800/60 hover:bg-slate-900/60 ${
                note.path === activePath ? 'bg-cyan-950/30 text-cyan-100' : 'text-slate-300'
              }`}
            >
              <div className="flex items-center gap-2">
                <FileText className="h-3.5 w-3.5 shrink-0 text-slate-500" />
                <span className="text-xs truncate">{note.name}</span>
              </div>
              <div className="text-[10px] text-slate-600 truncate pl-5">{note.path}</div>
            </button>
          ))}
          {filtered.length === 0 && <div className="p-4 text-center text-xs text-slate-500">No notes found.</div>}
        </div>
      </aside>

      <section className="copal-editor-leaf">
        <div className="copal-editor-titlebar">
          <div className="min-w-0 mr-auto">
            <div className="text-[11px] text-slate-500 font-mono truncate">{activePath || 'no note selected'}</div>
            <div className="text-sm font-semibold text-slate-100 truncate">{activeNote?.name ?? 'Select a vault file'}</div>
          </div>
          <Badge variant="outline" className="border-slate-700 text-slate-300 text-[10px]">L{cursorLine}</Badge>
          {targetLine && <Badge variant="outline" className="border-cyan-700 text-cyan-200 text-[10px]">jump L{targetLine}</Badge>}
          {dirty && <Badge variant="outline" className="border-amber-700 text-amber-300 text-[10px]">dirty</Badge>}
          <Button size="sm" variant="outline" className="h-8 border-slate-700 bg-slate-900" onClick={persistDraft} disabled={!activePath || !dirty}>
            <Save className="h-3.5 w-3.5 mr-1.5" />
            Save
          </Button>
          <Input value={renamePath} onChange={(event) => setRenamePath(event.target.value)} className="h-8 w-52 bg-slate-900 border-slate-700 text-xs" disabled={!activePath} />
          <Button size="sm" variant="outline" className="h-8 border-slate-700 bg-slate-900" onClick={renameActive} disabled={!activePath || !renamePath || renamePath === activePath}>
            <Pencil className="h-3.5 w-3.5 mr-1.5" />
            Rename
          </Button>
          <Button size="sm" variant="outline" className="h-8 border-red-900/60 bg-red-950/20 text-red-200" onClick={removeActive} disabled={!activePath}>
            <Trash2 className="h-3.5 w-3.5 mr-1.5" />
            Delete
          </Button>
        </div>

        <div className="copal-toolbar">
          <div className="copal-tool-group">
            <ModeButton mode="live" active={editorMode} onClick={setEditorMode} icon={<FileText className="h-3.5 w-3.5" />} />
            <ModeButton mode="source" active={editorMode} onClick={setEditorMode} icon={<PanelLeft className="h-3.5 w-3.5" />} />
            <ModeButton mode="reading" active={editorMode} onClick={setEditorMode} icon={<Eye className="h-3.5 w-3.5" />} />
          </div>
          <div className="copal-tool-group">
            {EDITOR_COMMANDS.slice(0, 12).map((command) => (
              <Button
                key={command.id}
                size="icon"
                variant="outline"
                className="h-7 w-7 border-slate-800 bg-slate-950"
                onClick={() => runEditorCommand(command.id)}
                disabled={!activePath}
                title={`${command.label}${command.shortcut ? ` (${command.shortcut})` : ''}`}
              >
                {command.icon}
              </Button>
            ))}
          </div>
          <div className="copal-tool-group">
            {[EDITOR_COMMANDS[16], EDITOR_COMMANDS[17], EDITOR_COMMANDS[18], EDITOR_COMMANDS[19], EDITOR_COMMANDS[24], EDITOR_COMMANDS[25], EDITOR_COMMANDS[27]]
              .filter((command): command is EditorCommandItem => Boolean(command))
              .map((command) => (
              <Button
                key={command.id}
                size="icon"
                variant="outline"
                className="h-7 w-7 border-slate-800 bg-slate-950"
                onClick={() => runEditorCommand(command.id)}
                disabled={!activePath}
                title={`${command.label}${command.shortcut ? ` (${command.shortcut})` : ''}`}
              >
                {command.icon}
              </Button>
            ))}
          </div>
          <div className="copal-tool-group">
            <Button size="icon" variant="outline" className="h-7 w-7 border-slate-800 bg-slate-950" onClick={() => openCommandPalette()} title="Command palette">
              <CommandIcon className="h-3.5 w-3.5" />
            </Button>
            <Button size="icon" variant="outline" className="h-7 w-7 border-slate-800 bg-slate-950" onClick={insertSlashCommand} disabled={!activePath} title="Slash insert">
              <Wand className="h-3.5 w-3.5" />
            </Button>
            <Button size="icon" variant="outline" className="h-7 w-7 border-slate-800 bg-slate-950" onClick={() => copyLinkToLine()} disabled={!activePath} title="Copy link to current line">
              <Clipboard className="h-3.5 w-3.5" />
            </Button>
          </div>
        </div>

        {commandOpen && (
          <div
            className="copal-command-surface"
            style={commandPoint ? { position: 'fixed', left: commandPoint.left, top: commandPoint.top, width: 'min(720px, calc(100vw - 16px))', zIndex: 60 } : undefined}
          >
            <div className="grid gap-2 xl:grid-cols-[260px_minmax(0,1fr)]">
              <div className="space-y-2">
                <div className="flex items-center gap-2">
                  <Keyboard className="h-3.5 w-3.5 text-slate-500" />
                  <Input
                    value={commandQuery}
                    onChange={(event) => setCommandQuery(event.target.value)}
                    placeholder="Command or slash insert..."
                    className="h-8 bg-slate-900 border-slate-700 text-xs"
                  />
                </div>
                <div className="text-[10px] text-slate-500 truncate">Current line: {currentLineText || 'blank'}</div>
              </div>
              <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3 max-h-44 overflow-y-auto pr-1">
                {visibleCommands.map((command) => (
                  <button
                    key={command.id}
                    onClick={() => {
                      runEditorCommand(command.id);
                      setCommandOpen(false);
                    }}
                    className="flex items-center gap-2 rounded-md border border-slate-800 bg-slate-900/60 px-2 py-1.5 text-left hover:border-cyan-800"
                  >
                    <span className="text-slate-500">{command.icon}</span>
                    <span className="min-w-0">
                      <span className="block text-[11px] text-slate-200 truncate">{command.label}</span>
                      <span className="block text-[10px] text-slate-600 truncate">{command.group}{command.shortcut ? ` / ${command.shortcut}` : ''}</span>
                    </span>
                  </button>
                ))}
              </div>
            </div>
            <div className="mt-3 grid gap-2 xl:grid-cols-2">
              <SuggestionPanel title="Wikilink targets">
                {noteSuggestions.map((note) => (
                  <button key={note.path} onClick={() => insertWikilink(noteSuggestionLabel(note.path))} className="rounded border border-slate-800 px-2 py-1 text-[10px] text-cyan-200 hover:border-cyan-800">
                    [[{noteSuggestionLabel(note.path)}]]
                  </button>
                ))}
              </SuggestionPanel>
              <SuggestionPanel title="Tags">
                {tagSuggestions.length > 0 ? (
                  tagSuggestions.map((tag) => (
                    <button key={tag} onClick={() => insertTag(tag)} className="rounded border border-slate-800 px-2 py-1 text-[10px] text-slate-300 hover:border-cyan-800">
                      #{tag}
                    </button>
                  ))
                ) : (
                  <span className="text-[10px] text-slate-600">No tags in current note.</span>
                )}
              </SuggestionPanel>
            </div>
          </div>
        )}

        {(error || notice) && (
          <div className="px-3 pt-3">
            {error && <div className="mb-2 rounded-md border border-red-500/40 bg-red-950/30 px-3 py-2 text-xs text-red-200">{error}</div>}
            {notice && <div className="mb-2 rounded-md border border-emerald-500/40 bg-emerald-950/20 px-3 py-2 text-xs text-emerald-200">{notice}</div>}
          </div>
        )}

        {activeNote ? (
          <div className="flex-1 min-h-0 grid grid-cols-1">
            {editorMode === 'reading' ? previewPane : editorPane}
          </div>
        ) : (
          <div className="h-full grid place-items-center text-xs text-slate-500">Select or create a vault file.</div>
        )}

        <div className="copal-statusbar">
          <span>{editorMode}</span>
          <span>L{cursorLine}</span>
          <span>{stats.lines} lines</span>
          <span>{stats.words} words</span>
          <span>{stats.done}/{stats.tasks} tasks</span>
          <span>{dirty ? 'unsaved' : 'saved'}</span>
          {activeNote && <span className="truncate">{activeNote.suffix || 'text'}</span>}
          {lastSaveBackup && <span className="truncate">backup {lastSaveBackup}</span>}
        </div>
      </section>

      <aside className="copal-pane border-t xl:border-t-0 xl:border-l border-slate-800">
        <div className="copal-pane-header">
          <div className="flex items-center gap-2">
            <PanelRight className="h-3.5 w-3.5 text-slate-500" />
            <div className="text-xs font-semibold text-slate-200 mr-auto">Linked panes</div>
          </div>
          <div className="mt-1 text-[10px] text-slate-500 truncate">{activePath || 'no active note'}</div>
        </div>
        <div className="copal-pane-tabs">
          {sidePanes.map((pane) => (
            <button
              key={pane.id}
              onClick={() => setSidePane(pane.id)}
              className={`copal-pane-tab ${sidePane === pane.id ? 'copal-pane-tab-active' : ''}`}
              title={pane.label}
            >
              <span className="inline-flex items-center gap-1">
                {pane.icon}
                {pane.label}
                <span className="text-slate-600">{pane.count}</span>
              </span>
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2 border-b border-slate-800 bg-slate-950/45 px-3 py-2">
          <Button size="icon" variant="outline" className="h-7 w-7 border-slate-700 bg-slate-900" onClick={() => downloadExport('/api/export/ai', 'copal-vault-ai-export.json')} title="AI export">
            <Download className="h-3.5 w-3.5" />
          </Button>
          <Button size="icon" variant="outline" className="h-7 w-7 border-slate-700 bg-slate-900" onClick={() => downloadExport('/api/export/markdown-bundle', 'copal-vault-markdown-bundle.zip')} title="Markdown bundle">
            <Download className="h-3.5 w-3.5" />
          </Button>
          <Button size="icon" variant="outline" className="h-7 w-7 border-slate-700 bg-slate-900" onClick={() => downloadExport('/api/export/okf', 'copal-vault-okf.json')} title="OKF export">
            <Download className="h-3.5 w-3.5" />
          </Button>
          <Button size="icon" variant="outline" className="h-7 w-7 border-slate-700 bg-slate-900" onClick={() => downloadExport('/api/export/doclang', 'copal-vault-doclang.dclg')} title="DocLang draft export">
            <Download className="h-3.5 w-3.5" />
          </Button>
          <span className="text-[10px] text-slate-500">exports</span>
        </div>
        <div className="flex-1 min-h-0 overflow-y-auto p-3">
        {parsed && (
          <div className="space-y-3">
            {sidePane === 'properties' && (
              <>
                <Panel title="Properties">
                  {parsed.frontmatter ? (
                    <div className="space-y-1">
                      {Object.entries(parsed.frontmatter.values).map(([key, value]) => (
                        <div key={key} className="copal-property-row">
                          <span className="text-slate-500 font-mono truncate">{key}</span>
                          <span className="text-slate-300 break-all">{Array.isArray(value) ? value.join(', ') : String(value)}</span>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="text-[11px] text-slate-500">No frontmatter.</div>
                  )}
                </Panel>
                <Panel title="Document">
                  <div className="space-y-1 text-[11px] text-slate-400">
                    <IconRow icon={<FileText className="h-3 w-3" />} label={`${stats.lines} lines`} />
                    <IconRow icon={<BookOpen className="h-3 w-3" />} label={`${stats.words} words`} />
                    <IconRow icon={<ListChecks className="h-3 w-3" />} label={`${stats.done}/${stats.tasks} tasks done`} />
                  </div>
                </Panel>
                {lastSaveBackup && (
                  <Panel title="Last save">
                    <div className="text-[11px] text-slate-400 break-all">Backup: {lastSaveBackup}</div>
                  </Panel>
                )}
              </>
            )}

            {sidePane === 'outline' && (
              <>
                <Panel title="Outline and blocks">
                  <div className="space-y-1">
                    {parsed.outline.slice(0, 48).map((item) => (
                      <button
                        key={`${item.kind}-${item.line}-${item.text}`}
                        onClick={() => focusLine(item.line)}
                        className="block w-full rounded px-1 py-0.5 text-left text-[11px] text-slate-300 hover:bg-slate-900 hover:text-cyan-200 truncate"
                        style={{ paddingLeft: `${4 + Math.min(item.level - 1, 5) * 8}px` }}
                      >
                        <span className="font-mono text-slate-600">L{item.line}</span> {item.kind === 'heading' ? '# ' : '- '}
                        {item.text}
                      </button>
                    ))}
                    {parsed.outline.length === 0 && <div className="text-[11px] text-slate-500">No outline yet.</div>}
                  </div>
                </Panel>

                <Panel title="Search results">
                  <div className="space-y-1">
                    {searchResults.map((result) => (
                      <button key={result.path} onClick={() => loadNote(result.path)} className="block w-full text-left border border-slate-800 rounded-md p-2 hover:border-cyan-900/70">
                        <div className="text-[11px] text-cyan-200 truncate">{result.path}</div>
                        <div className="text-[10px] text-slate-500 line-clamp-2">{result.excerpt}</div>
                      </button>
                    ))}
                    {searchResults.length === 0 && <div className="text-[11px] text-slate-500">No content search results.</div>}
                  </div>
                </Panel>
              </>
            )}

            {sidePane === 'links' && (
              <>
                <Panel title="Backlinks">
                  <div className="space-y-1">
                    {backlinks.map((link) => (
                      <button
                        key={`${link.sourcePath}-${link.type}-${link.line ?? 0}`}
                        onClick={() => loadNote(link.sourcePath, link.line)}
                        className="block w-full rounded px-1 py-0.5 text-left text-[11px] text-cyan-200 hover:bg-slate-900 truncate"
                      >
                        {link.sourceTitle}{link.line ? `:L${link.line}` : ''}
                      </button>
                    ))}
                    {backlinks.length === 0 && <div className="text-[11px] text-slate-500">No backlinks.</div>}
                  </div>
                </Panel>

                <Panel title="Outgoing links">
                  <IconRow icon={<Link2 className="h-3 w-3" />} label={`${parsed.wikilinks.length} wikilinks`} />
                  <div className="flex flex-wrap gap-1 mt-2">
                    {parsed.wikilinks.map((link) => <Badge key={link} variant="outline" className="border-cyan-700/60 text-cyan-200 text-[10px]">[[{link}]]</Badge>)}
                    {parsed.wikilinks.length === 0 && <span className="text-[11px] text-slate-500">No wikilinks.</span>}
                  </div>
                </Panel>

                <Panel title="Tags">
                  <IconRow icon={<Tags className="h-3 w-3" />} label={`${parsed.tags.length} tags`} />
                  <div className="flex flex-wrap gap-1 mt-2">
                    {parsed.tags.map((tag) => <Badge key={tag} variant="outline" className="border-slate-600 text-slate-300 text-[10px]">#{tag}</Badge>)}
                    {parsed.tags.length === 0 && <span className="text-[11px] text-slate-500">No tags.</span>}
                  </div>
                </Panel>
              </>
            )}

            {sidePane === 'tasks' && (
              <Panel title="Markdown tasks">
                <IconRow icon={<ListChecks className="h-3 w-3" />} label={`${parsed.tasks.length} tasks`} />
                <div className="space-y-1 mt-2">
                  {parsed.tasks.map((task) => (
                    <button key={`${task.line}-${task.text}`} onClick={() => focusLine(task.line)} className="block w-full rounded px-1 py-0.5 text-left text-[11px] text-slate-300 hover:bg-slate-900">
                      <span className="text-slate-600 font-mono">L{task.line}</span>{' '}
                      <span className={task.status === 'done' ? 'line-through text-slate-500' : ''}>[{task.statusSymbol}] {task.text}</span>
                    </button>
                  ))}
                  {parsed.tasks.length === 0 && <div className="text-[11px] text-slate-500">No markdown tasks.</div>}
                </div>
              </Panel>
            )}

            {sidePane === 'queries' && (
              <>
                <Panel title="Tables and queries">
                  <div className="space-y-1 text-[11px] text-slate-400">
                    {parsed.tables.map((table) => (
                      <button key={`table-${table.line}`} onClick={() => focusLine(table.line)} className="block w-full text-left text-cyan-200">
                        L{table.line} table: {table.headers.join(', ')}
                      </button>
                    ))}
                    {parsed.dataviewBlocks.map((block) => (
                      <button key={`dataview-${block.line}`} onClick={() => focusLine(block.line)} className="block w-full text-left text-purple-200">
                        L{block.line} dataview block
                      </button>
                    ))}
                    {parsed.taskQueryBlocks.map((block) => (
                      <button key={`tasks-${block.line}`} onClick={() => focusLine(block.line)} className="block w-full text-left text-emerald-200">
                        L{block.line} tasks query block
                      </button>
                    ))}
                    {parsed.tables.length === 0 && parsed.dataviewBlocks.length === 0 && parsed.taskQueryBlocks.length === 0 && (
                      <div className="text-slate-500">No tables or query blocks.</div>
                    )}
                  </div>
                </Panel>

                <Panel title="Plugin syntax">
                  <div className="space-y-1 text-[11px] text-slate-400">
                    {parsed.fencedBlocks.map((block) => <div key={`${block.lang}-${block.line}`}><span className="font-mono text-slate-500">L{block.line}</span> fenced `{block.lang}`</div>)}
                    {parsed.hasTemplater && <div>Templater tags detected and preserved inert.</div>}
                    {!parsed.hasTemplater && parsed.fencedBlocks.length === 0 && <div className="text-slate-500">No plugin syntax detected.</div>}
                  </div>
                </Panel>
              </>
            )}
          </div>
        )}
        {!parsed && <div className="text-[11px] text-slate-500">Select a note to inspect linked panes.</div>}
        </div>
      </aside>
    </div>
  );
}

function ModeButton({ mode, active, icon, onClick }: { mode: EditorMode; active: EditorMode; icon: ReactNode; onClick: (mode: EditorMode) => void }) {
  const selected = mode === active;
  return (
    <Button
      size="sm"
      variant="outline"
      className={`h-8 border-slate-700 ${selected ? 'bg-cyan-950/50 text-cyan-100' : 'bg-slate-900'}`}
      onClick={() => onClick(mode)}
      title={`${mode} mode`}
    >
      {icon}
      <span className="ml-1.5 capitalize">{mode}</span>
    </Button>
  );
}

function SuggestionPanel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="rounded-md border border-slate-800 bg-slate-900/40 p-2">
      <div className="mb-2 text-[10px] uppercase tracking-wide text-slate-500">{title}</div>
      <div className="flex flex-wrap gap-1">{children}</div>
    </div>
  );
}

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="rounded-md border border-slate-700/60 bg-slate-900/40 p-2">
      <div className="text-[11px] uppercase tracking-wide text-slate-500 mb-2">{title}</div>
      {children}
    </div>
  );
}

function IconRow({ icon, label }: { icon: ReactNode; label: string }) {
  return (
    <div className="flex items-center gap-1.5 text-[11px] text-slate-300">
      <span className="text-slate-500">{icon}</span>
      {label}
    </div>
  );
}
