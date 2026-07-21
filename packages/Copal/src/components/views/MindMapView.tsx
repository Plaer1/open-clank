'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ChevronRight,
  ChevronDown,
  ExternalLink,
  GitFork,
  Plus,
  Trash2,
  RefreshCw,
  AlertTriangle,
} from 'lucide-react';
import { useVaultIndex } from '@/hooks/useVaultIndex';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { openNoteAt } from '@/lib/noteNavigation';
import {
  outlineEntries,
  outlineTree,
  flattenTree,
  reparentHeading,
  moveHeadingSection,
  moveHeadingSectionTo,
} from '../../../static/js/copal/notesModel';

/* ── Tree node type ── */
interface TreeNode {
  line: number;
  level: number;
  text: string;
  kind: 'heading' | 'list';
  children: TreeNode[];
  collapsed?: boolean;
}

/* ── Virtual list constants ── */
const ROW_HEIGHT = 36;
const BUFFER = 8;

/* ── Conflict dialog ── */
function ConflictDialog({
  open,
  onOverwrite,
  onKeepMine,
  onCancel,
}: {
  open: boolean;
  onOverwrite: () => void;
  onKeepMine: () => void;
  onCancel: () => void;
}) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div className="bg-slate-900 border border-slate-700 rounded-lg p-5 w-[380px] shadow-xl">
        <div className="flex items-center gap-2 mb-3">
          <AlertTriangle className="h-5 w-5 text-amber-400" />
          <span className="text-sm font-semibold text-slate-100">Conflict detected</span>
        </div>
        <p className="text-xs text-slate-400 mb-4">
          The document has been modified since it was loaded. Choose how to resolve:
        </p>
        <div className="flex gap-2 justify-end">
          <Button variant="outline" size="sm" className="border-slate-700 text-slate-300" onClick={onCancel}>
            Cancel
          </Button>
          <Button variant="outline" size="sm" className="border-slate-700 text-slate-300" onClick={onKeepMine}>
            Keep mine
          </Button>
          <Button size="sm" className="bg-cyan-600 hover:bg-cyan-500 text-white" onClick={onOverwrite}>
            Overwrite
          </Button>
        </div>
      </div>
    </div>
  );
}

/* ── Single tree row ── */
function TreeRow({
  node,
  depth,
  selected,
  editingLine,
  editValue,
  onSelect,
  onToggle,
  onRenameStart,
  onRenameCommit,
  onRenameCancel,
  onEditChange,
  onDelete,
  onAddChild,
  onIndent,
  onDedent,
  onMoveUp,
  onMoveDown,
  onOpenSource,
  docPath,
}: {
  node: TreeNode;
  depth: number;
  selected: number | null;
  editingLine: number | null;
  editValue: string;
  onSelect: (line: number) => void;
  onToggle: (line: number) => void;
  onRenameStart: (line: number, text: string) => void;
  onRenameCommit: () => void;
  onRenameCancel: () => void;
  onEditChange: (value: string) => void;
  onDelete: (line: number) => void;
  onAddChild: (line: number) => void;
  onIndent: (line: number) => void;
  onDedent: (line: number) => void;
  onMoveUp: (line: number) => void;
  onMoveDown: (line: number) => void;
  onOpenSource: (path: string, line: number) => void;
  docPath: string;
}) {
  const hasChildren = node.children.length > 0;
  const isEditing = editingLine === node.line;
  const isSelected = selected === node.line;

  return (
    <div
      className={`flex items-center gap-1 px-2 py-1 text-xs border-b border-slate-800/40 group cursor-pointer select-none ${
        isSelected ? 'bg-cyan-950/30 text-cyan-100' : 'text-slate-300 hover:bg-slate-900/60'
      }`}
      style={{ paddingLeft: `${depth * 20 + 8}px` }}
      onClick={() => onSelect(node.line)}
      onDoubleClick={() => onRenameStart(node.line, node.text)}
    >
      {/* Collapse toggle */}
      <button
        className="w-4 h-4 flex items-center justify-center shrink-0 text-slate-500 hover:text-slate-300"
        onClick={(e) => {
          e.stopPropagation();
          if (hasChildren) onToggle(node.line);
        }}
      >
        {hasChildren ? (
          node.collapsed ? (
            <ChevronRight className="h-3 w-3" />
          ) : (
            <ChevronDown className="h-3 w-3" />
          )
        ) : (
          <span className="w-3" />
        )}
      </button>

      {/* Heading level indicator */}
      <span className="text-[10px] text-slate-600 font-mono w-4 shrink-0">
        {'#'.repeat(Math.min(node.level, 6))}
      </span>

      {/* Text or edit input */}
      {isEditing ? (
        <Input
          value={editValue}
          onChange={(e) => onEditChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') onRenameCommit();
            if (e.key === 'Escape') onRenameCancel();
          }}
          onBlur={onRenameCommit}
          className="h-6 text-xs bg-slate-800 border-cyan-600 flex-1"
          autoFocus
          onClick={(e) => e.stopPropagation()}
        />
      ) : (
        <span className="flex-1 truncate">{node.text}</span>
      )}

      {/* Action buttons (visible on hover) */}
      <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity shrink-0">
        <button
          className="p-0.5 text-slate-500 hover:text-cyan-300"
          title="Add child heading"
          onClick={(e) => {
            e.stopPropagation();
            onAddChild(node.line);
          }}
        >
          <Plus className="h-3 w-3" />
        </button>
        <button
          className="p-0.5 text-slate-500 hover:text-red-400"
          title="Delete heading"
          onClick={(e) => {
            e.stopPropagation();
            onDelete(node.line);
          }}
        >
          <Trash2 className="h-3 w-3" />
        </button>
        <button
          className="p-0.5 text-slate-500 hover:text-slate-300"
          title="Open in source"
          onClick={(e) => {
            e.stopPropagation();
            onOpenSource(docPath, node.line);
          }}
        >
          <ExternalLink className="h-3 w-3" />
        </button>
      </div>
    </div>
  );
}

/* ── Main MindMapView ── */
export function MindMapView() {
  const { index, loading, error, refresh } = useVaultIndex();
  const [activePath, setActivePath] = useState('');
  const [query, setQuery] = useState('');
  const [tree, setTree] = useState<TreeNode[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [editingLine, setEditingLine] = useState<number | null>(null);
  const [editValue, setEditValue] = useState('');
  const [collapsed, setCollapsed] = useState<Set<number>>(new Set());
  const [scrollTop, setScrollTop] = useState(0);
  const [conflictOpen, setConflictOpen] = useState(false);
  const [pendingSave, setPendingSave] = useState<{ content: string } | null>(null);
  const [loadedMtime, setLoadedMtime] = useState<number>(0);
  const scrollRef = useRef<HTMLDivElement>(null);

  const notes = useMemo(
    () => index.notes.filter((n) => n.parsed.outline.length > 0),
    [index.notes],
  );
  const active = useMemo(
    () => notes.find((n) => n.path === activePath) ?? notes[0],
    [notes, activePath],
  );

  // Build tree from outline
  const buildTree = useCallback(
    (note: typeof active) => {
      if (!note) return [];
      const flat = outlineEntries(note.content);
      return outlineTree(flat);
    },
    [],
  );

  // Initialize tree when active note changes
  useEffect(() => {
    if (active) {
      setTree(buildTree(active));
      setLoadedMtime(active.mtime);
      setSelected(null);
      setCollapsed(new Set());
      setEditingLine(null);
      if (scrollRef.current) scrollRef.current.scrollTop = 0;
      setScrollTop(0);
    }
  }, [active?.path, buildTree]);

  // Filter tree by query
  const filteredTree = useMemo(() => {
    if (!query.trim()) return tree;
    const q = query.toLowerCase();
    function filterNodes(nodes: TreeNode[]): TreeNode[] {
      return nodes
        .filter((n) => n.text.toLowerCase().includes(q))
        .map((n) => ({ ...n, children: filterNodes(n.children) }));
    }
    return filterNodes(tree);
  }, [tree, query]);

  // Flatten visible nodes for virtual list
  const visibleNodes = useMemo(() => {
    const result: { node: TreeNode; depth: number }[] = [];
    function walk(nodes: TreeNode[], depth: number) {
      for (const node of nodes) {
        result.push({ node, depth });
        if (!collapsed.has(node.line) && node.children.length) {
          walk(node.children, depth + 1);
        }
      }
    }
    walk(filteredTree, 0);
    return result;
  }, [filteredTree, collapsed]);

  const totalHeight = visibleNodes.length * ROW_HEIGHT;
  const containerHeight = scrollRef.current?.clientHeight ?? 600;
  const startIndex = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - BUFFER);
  const endIndex = Math.min(
    visibleNodes.length,
    Math.ceil((scrollTop + containerHeight) / ROW_HEIGHT) + BUFFER,
  );
  const visibleSlice = visibleNodes.slice(startIndex, endIndex);

  // Toggle collapse
  const toggleCollapse = useCallback((line: number) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(line)) next.delete(line);
      else next.add(line);
      return next;
    });
  }, []);

  // Save helper (with conflict detection)
  const saveContent = useCallback(
    async (content: string) => {
      if (!active) return;
      try {
        // Check for conflict: re-fetch current mtime
        const res = await fetch(`/api/note?path=${encodeURIComponent(active.path)}`);
        if (res.ok) {
          const current = await res.json();
          if (current.mtime > loadedMtime) {
            // Conflict detected
            setPendingSave({ content });
            setConflictOpen(true);
            return;
          }
        }
        // No conflict, save directly
        await fetch('/api/note', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: active.path, content }),
        });
        // Update mtime after successful save
        const updated = await fetch(`/api/note?path=${encodeURIComponent(active.path)}`);
        if (updated.ok) {
          const data = await updated.json();
          setLoadedMtime(data.mtime);
        }
      } catch (err) {
        console.error('Save failed:', err);
      }
    },
    [active, loadedMtime],
  );

  // Regenerate markdown from tree
  const treeToMarkdown = useCallback(
    (nodes: TreeNode[], originalContent: string): string => {
      const lines = originalContent.split('\n');
      // Collect all heading lines from the tree in order
      const treeHeadings: { line: number; level: number; text: string }[] = [];
      function collect(nodes: TreeNode[]) {
        for (const node of nodes) {
          treeHeadings.push({ line: node.line, level: node.level, text: node.text });
          collect(node.children);
        }
      }
      collect(nodes);
      // Apply heading changes to original content
      for (const heading of treeHeadings) {
        const idx = heading.line - 1;
        if (idx >= 0 && idx < lines.length) {
          lines[idx] = `${'#'.repeat(heading.level)} ${heading.text}`;
        }
      }
      return lines.join('\n');
    },
    [],
  );

  // Commit rename
  const commitRename = useCallback(() => {
    if (editingLine === null || !active) return;
    const newTree = JSON.parse(JSON.stringify(tree)) as TreeNode[];
    function findAndUpdate(nodes: TreeNode[]): boolean {
      for (const node of nodes) {
        if (node.line === editingLine) {
          node.text = editValue.trim() || node.text;
          return true;
        }
        if (findAndUpdate(node.children)) return true;
      }
      return false;
    }
    findAndUpdate(newTree);
    setTree(newTree);
    setEditingLine(null);
    const md = treeToMarkdown(newTree, active.content);
    saveContent(md);
  }, [editingLine, editValue, tree, active, treeToMarkdown, saveContent]);

  // Delete heading
  const deleteHeading = useCallback(
    (line: number) => {
      if (!active) return;
      // Use moveHeadingSection to remove by setting text to empty approach
      // Simpler: rebuild content without this heading's section
      const lines = active.content.split('\n');
      const entries = outlineEntries(active.content);
      const idx = entries.findIndex((e) => e.line === line);
      if (idx < 0) return;
      const entry = entries[idx];
      // Find section end
      let endLine = lines.length;
      for (let i = idx + 1; i < entries.length; i++) {
        if (entries[i].level <= entry.level) {
          endLine = entries[i].line - 1;
          break;
        }
      }
      // Remove lines from heading to end of section
      const newLines = [...lines.slice(0, line - 1), ...lines.slice(endLine)];
      const newContent = newLines.join('\n');
      const newEntries = outlineEntries(newContent);
      setTree(outlineTree(newEntries));
      saveContent(newContent);
      if (active.content !== newContent) {
        // Update active content locally
        active.content = newContent;
      }
    },
    [active, saveContent],
  );

  // Add child heading
  const addChildHeading = useCallback(
    (parentLine: number) => {
      if (!active) return;
      const newContent = reparentHeading(
        active.content,
        parentLine,
        0, // dummy, we'll insert after
      );
      // Insert a new heading after the parent's section
      const lines = active.content.split('\n');
      const entries = outlineEntries(active.content);
      const parentIdx = entries.findIndex((e) => e.line === parentLine);
      if (parentIdx < 0) return;
      const parent = entries[parentIdx];
      // Find end of parent section
      let insertAfter = parent.line;
      for (let i = parentIdx + 1; i < entries.length; i++) {
        if (entries[i].level <= parent.level) break;
        insertAfter = entries[i].line;
      }
      const newHeading = `${'#'.repeat(Math.min(parent.level + 1, 6))} New heading`;
      const allLines = active.content.split('\n');
      allLines.splice(insertAfter, 0, newHeading);
      const content = allLines.join('\n');
      const newEntries = outlineEntries(content);
      setTree(outlineTree(newEntries));
      active.content = content;
      saveContent(content);
    },
    [active, saveContent],
  );

  // Indent (Tab)
  const indentHeading = useCallback(
    (line: number) => {
      if (!active) return;
      const entries = outlineEntries(active.content);
      const entry = entries.find((e) => e.line === line);
      if (!entry || entry.level >= 6) return;
      const newContent = reparentHeading(active.content, line, entry.level + 1);
      const newEntries = outlineEntries(newContent);
      setTree(outlineTree(newEntries));
      active.content = newContent;
      saveContent(newContent);
    },
    [active, saveContent],
  );

  // Dedent (Shift+Tab)
  const dedentHeading = useCallback(
    (line: number) => {
      if (!active) return;
      const entries = outlineEntries(active.content);
      const entry = entries.find((e) => e.line === line);
      if (!entry || entry.level <= 1) return;
      const newContent = reparentHeading(active.content, line, entry.level - 1);
      const newEntries = outlineEntries(newContent);
      setTree(outlineTree(newEntries));
      active.content = newContent;
      saveContent(newContent);
    },
    [active, saveContent],
  );

  // Move up
  const moveUp = useCallback(
    (line: number) => {
      if (!active) return;
      const newContent = moveHeadingSection(active.content, line, -1);
      if (newContent === active.content) return;
      const newEntries = outlineEntries(newContent);
      setTree(outlineTree(newEntries));
      active.content = newContent;
      saveContent(newContent);
    },
    [active, saveContent],
  );

  // Move down
  const moveDown = useCallback(
    (line: number) => {
      if (!active) return;
      const newContent = moveHeadingSection(active.content, line, 1);
      if (newContent === active.content) return;
      const newEntries = outlineEntries(newContent);
      setTree(outlineTree(newEntries));
      active.content = newContent;
      saveContent(newContent);
    },
    [active, saveContent],
  );

  // Keyboard navigation
  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (editingLine !== null) return;
      if (selected === null) return;

      const visibleLines = visibleNodes.map((v) => v.node.line);
      const currentIdx = visibleLines.indexOf(selected);

      switch (e.key) {
        case 'ArrowDown': {
          e.preventDefault();
          if (currentIdx < visibleLines.length - 1) setSelected(visibleLines[currentIdx + 1]);
          break;
        }
        case 'ArrowUp': {
          e.preventDefault();
          if (currentIdx > 0) setSelected(visibleLines[currentIdx - 1]);
          break;
        }
        case 'ArrowRight': {
          e.preventDefault();
          // Expand if collapsed, else move to first child
          const node = visibleNodes[currentIdx]?.node;
          if (node) {
            if (collapsed.has(node.line) && node.children.length) {
              toggleCollapse(node.line);
            } else if (node.children.length && !collapsed.has(node.line)) {
              setSelected(node.children[0].line);
            }
          }
          break;
        }
        case 'ArrowLeft': {
          e.preventDefault();
          const node = visibleNodes[currentIdx]?.node;
          if (node) {
            if (!collapsed.has(node.line) && node.children.length) {
              toggleCollapse(node.line);
            }
          }
          break;
        }
        case 'Tab': {
          e.preventDefault();
          if (e.shiftKey) dedentHeading(selected);
          else indentHeading(selected);
          break;
        }
        case 'Enter': {
          e.preventDefault();
          const node = visibleNodes[currentIdx]?.node;
          if (node) {
            setEditingLine(node.line);
            setEditValue(node.text);
          }
          break;
        }
        case 'F2': {
          e.preventDefault();
          const node = visibleNodes[currentIdx]?.node;
          if (node) {
            setEditingLine(node.line);
            setEditValue(node.text);
          }
          break;
        }
        case 'Delete':
        case 'Backspace': {
          if (!e.ctrlKey && !e.metaKey) break;
          e.preventDefault();
          deleteHeading(selected);
          break;
        }
      }
    }
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [selected, editingLine, visibleNodes, collapsed, toggleCollapse, indentHeading, dedentHeading, deleteHeading]);

  // Scroll selected into view
  useEffect(() => {
    if (selected === null || !scrollRef.current) return;
    const idx = visibleNodes.findIndex((v) => v.node.line === selected);
    if (idx < 0) return;
    const top = idx * ROW_HEIGHT;
    const bottom = top + ROW_HEIGHT;
    const el = scrollRef.current;
    if (top < el.scrollTop) el.scrollTop = top;
    else if (bottom > el.scrollTop + el.clientHeight) el.scrollTop = bottom - el.clientHeight;
  }, [selected, visibleNodes]);

  // Conflict resolution handlers
  const handleOverwrite = useCallback(async () => {
    if (!active || !pendingSave) return;
    await fetch('/api/note', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: active.path, content: pendingSave.content }),
    });
    const updated = await fetch(`/api/note?path=${encodeURIComponent(active.path)}`);
    if (updated.ok) {
      const data = await updated.json();
      setLoadedMtime(data.mtime);
    }
    setConflictOpen(false);
    setPendingSave(null);
  }, [active, pendingSave]);

  const handleKeepMine = useCallback(async () => {
    // For now, "keep mine" means merge: apply our heading text changes to the remote content
    // This is a best-effort merge — apply our heading renames to whatever the current file is
    if (!active || !pendingSave) return;
    try {
      const res = await fetch(`/api/note?path=${encodeURIComponent(active.path)}`);
      if (!res.ok) return;
      const remote = await res.json();
      const remoteEntries = outlineEntries(remote.content);
      const localEntries = outlineEntries(pendingSave.content);
      // Build a map of line->text from our edits
      const localTextByLine = new Map<number, { level: number; text: string }>();
      for (const e of localEntries) localTextByLine.set(e.line, { level: e.level, text: e.text });
      // Apply our text changes to remote content
      const lines = remote.content.split('\n');
      for (const remoteEntry of remoteEntries) {
        const local = localTextByLine.get(remoteEntry.line);
        if (local && local.text !== remoteEntry.text) {
          const idx = remoteEntry.line - 1;
          if (idx >= 0 && idx < lines.length) {
            lines[idx] = `${'#'.repeat(local.level)} ${local.text}`;
          }
        }
      }
      const merged = lines.join('\n');
      await fetch('/api/note', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: active.path, content: merged }),
      });
      const updated = await fetch(`/api/note?path=${encodeURIComponent(active.path)}`);
      if (updated.ok) {
        const data = await updated.json();
        setLoadedMtime(data.mtime);
        active.content = merged;
        const newEntries = outlineEntries(merged);
        setTree(outlineTree(newEntries));
      }
    } catch (err) {
      console.error('Merge failed:', err);
    }
    setConflictOpen(false);
    setPendingSave(null);
  }, [active, pendingSave]);

  const handleCancelConflict = useCallback(() => {
    setConflictOpen(false);
    setPendingSave(null);
  }, []);

  return (
    <div className="w-full h-full rounded-xl border border-slate-700/50 bg-slate-950/40 overflow-hidden grid grid-cols-1 lg:grid-cols-[280px_minmax(0,1fr)]">
      {/* Sidebar: document picker */}
      <aside className="border-b lg:border-b-0 lg:border-r border-slate-700/50 bg-slate-950/50 min-h-0 flex flex-col">
        <div className="p-3 border-b border-slate-700/50 flex items-center gap-2">
          <GitFork className="h-4 w-4 text-cyan-300" />
          <div className="text-xs font-semibold text-slate-200">Mind map</div>
          <Button size="icon" variant="outline" className="ml-auto h-7 w-7 border-slate-700 bg-slate-900" onClick={refresh}>
            <RefreshCw className="h-3.5 w-3.5" />
          </Button>
        </div>
        <div className="flex-1 overflow-y-auto">
          {notes.map((note) => (
            <button
              key={note.path}
              onClick={() => setActivePath(note.path)}
              className={`w-full text-left px-3 py-2 border-b border-slate-800/60 text-xs hover:bg-slate-900/60 ${
                active?.path === note.path ? 'text-cyan-100 bg-cyan-950/30' : 'text-slate-300'
              }`}
            >
              <div className="truncate">{note.title}</div>
              <div className="text-[10px] text-slate-600">{note.parsed.outline.length} headings</div>
            </button>
          ))}
        </div>
      </aside>

      {/* Main: tree view */}
      <section className="min-h-0 flex flex-col">
        {error && <div className="m-3 text-xs text-red-200 border border-red-500/40 bg-red-950/30 rounded-md p-2">{error}</div>}
        {loading && <div className="p-4 text-xs text-slate-500">Loading outlines...</div>}

        {active && (
          <>
            {/* Header */}
            <div className="flex flex-wrap items-center gap-2 p-3 border-b border-slate-700/50">
              <div className="min-w-0">
                <div className="text-[11px] text-slate-500 font-mono truncate">{active.path}</div>
                <h2 className="text-lg font-semibold text-slate-100 truncate">{active.title}</h2>
              </div>
              <Input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Filter headings..."
                className="ml-auto h-8 w-48 bg-slate-900 border-slate-700 text-xs shrink-0"
              />
              <div className="text-[10px] text-slate-600 shrink-0">
                {visibleNodes.length} headings
              </div>
            </div>

            {/* Virtualized tree */}
            <div
              ref={scrollRef}
              className="flex-1 overflow-y-auto"
              onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
            >
              {visibleNodes.length === 0 && (
                <div className="p-8 text-center text-xs text-slate-500">
                  {query ? 'No headings match your filter.' : 'No headings found in this document.'}
                </div>
              )}
              <div style={{ height: `${totalHeight}px`, position: 'relative' }}>
                {visibleSlice.map((item, i) => (
                  <div
                    key={item.node.line}
                    style={{
                      position: 'absolute',
                      top: `${(startIndex + i) * ROW_HEIGHT}px`,
                      left: 0,
                      right: 0,
                      height: `${ROW_HEIGHT}px`,
                    }}
                  >
                    <TreeRow
                      node={item.node}
                      depth={item.depth}
                      selected={selected}
                      editingLine={editingLine}
                      editValue={editValue}
                      onSelect={setSelected}
                      onToggle={toggleCollapse}
                      onRenameStart={(line, text) => {
                        setEditingLine(line);
                        setEditValue(text);
                      }}
                      onRenameCommit={commitRename}
                      onRenameCancel={() => setEditingLine(null)}
                      onEditChange={setEditValue}
                      onDelete={deleteHeading}
                      onAddChild={addChildHeading}
                      onIndent={indentHeading}
                      onDedent={dedentHeading}
                      onMoveUp={moveUp}
                      onMoveDown={moveDown}
                      onOpenSource={openNoteAt}
                      docPath={active.path}
                    />
                  </div>
                ))}
              </div>
            </div>

            {/* Keyboard hint */}
            <div className="px-3 py-1.5 border-t border-slate-700/50 text-[10px] text-slate-600 flex gap-4 shrink-0">
              <span>↑↓ navigate</span>
              <span>←→ expand/collapse</span>
              <span>Tab/Shift+Tab indent</span>
              <span>Enter/F2 rename</span>
              <span>Ctrl+Del delete</span>
            </div>
          </>
        )}
      </section>

      {/* Conflict dialog */}
      <ConflictDialog
        open={conflictOpen}
        onOverwrite={handleOverwrite}
        onKeepMine={handleKeepMine}
        onCancel={handleCancelConflict}
      />
    </div>
  );
}
