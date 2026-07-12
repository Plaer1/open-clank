'use client';

import { useMemo, useRef, useState } from 'react';
import { useMoveStore } from '@/store/useMoveStore';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Checkbox } from '@/components/ui/checkbox';
import { CheckCircle2, Circle, Plus, Trash2, Pencil, ListTodo } from 'lucide-react';

/**
 * Sidebar panel for "floating to-dos" — items that have no track, no date,
 * and no timeline presence. Just a simple checklist.
 */
export function FloatingTodosPanel() {
  const todos = useMoveStore((s) => s.data.floatingTodos ?? []);
  const addFloatingTodo = useMoveStore((s) => s.addFloatingTodo);
  const toggleFloatingTodo = useMoveStore((s) => s.toggleFloatingTodo);
  const removeFloatingTodo = useMoveStore((s) => s.removeFloatingTodo);
  const updateFloatingTodo = useMoveStore((s) => s.updateFloatingTodo);
  const [draft, setDraft] = useState('');
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editText, setEditText] = useState('');
  const editInputRef = useRef<HTMLInputElement>(null);

  const sorted = useMemo(() => {
    // Incomplete first, then by original order
    return [...todos].sort((a, b) => {
      if (a.done !== b.done) return a.done ? 1 : -1;
      return 0;
    });
  }, [todos]);

  function commit() {
    const text = draft.trim();
    if (!text) return;
    addFloatingTodo(text);
    setDraft('');
  }

  function startEdit(id: string, currentText: string) {
    setEditingId(id);
    setEditText(currentText);
    // Focus input after render
    setTimeout(() => editInputRef.current?.focus(), 0);
  }

  function saveEdit() {
    if (editingId && editText.trim()) {
      updateFloatingTodo(editingId, editText.trim());
    }
    setEditingId(null);
    setEditText('');
  }

  function cancelEdit() {
    setEditingId(null);
    setEditText('');
  }

  return (
    <div className="flex flex-col gap-2 p-3 rounded-xl border border-slate-700/50 bg-slate-950/40">
      <div className="flex items-center gap-1.5">
        <ListTodo className="w-3.5 h-3.5 text-slate-400" />
        <h3 className="text-sm font-semibold text-slate-200 uppercase tracking-wide">
          To-Dos
        </h3>
        <span className="text-[10px] text-slate-500">no line</span>
      </div>

      <div className="text-[10px] text-slate-500 leading-relaxed -mt-1">
        Items with no track, no date, no timeline presence. Just a checklist.
      </div>

      {/* Add new */}
      <div className="flex gap-1.5">
        <Input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') commit();
          }}
          placeholder="Add a to-do…"
          className="h-8 text-xs bg-slate-900 border-slate-700 text-slate-200 placeholder:text-slate-600"
        />
        <Button
          size="sm"
          variant="ghost"
          className="h-8 px-2 text-slate-300 hover:text-white hover:bg-slate-800"
          onClick={commit}
          aria-label="Add to-do"
        >
          <Plus className="w-3.5 h-3.5" />
        </Button>
      </div>

      {/* List */}
      {sorted.length === 0 ? (
        <div className="text-[11px] text-slate-600 italic px-1 py-2">
          Nothing here yet.
        </div>
      ) : (
        <ul className="flex flex-col gap-1 max-h-48 overflow-y-auto pr-1">
          {sorted.map((todo) => (
            <li
              key={todo.id}
              className="group flex items-center gap-2 px-1.5 py-1 rounded hover:bg-slate-800/40"
            >
              <Checkbox
                checked={todo.done}
                onCheckedChange={() => toggleFloatingTodo(todo.id)}
                className="border-slate-600 data-[state=checked]:bg-cyan-500 data-[state=checked]:border-cyan-500"
              />
              {editingId === todo.id ? (
                <Input
                  ref={editInputRef}
                  value={editText}
                  onChange={(e) => setEditText(e.target.value)}
                  onBlur={saveEdit}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') saveEdit();
                    if (e.key === 'Escape') cancelEdit();
                  }}
                  className="h-6 text-xs bg-slate-900 border-slate-700 text-slate-200 flex-1"
                />
              ) : (
                <span
                  className={`flex-1 text-xs leading-tight ${
                    todo.done
                      ? 'text-slate-500 line-through'
                      : 'text-slate-200'
                  }`}
                  onDoubleClick={() => startEdit(todo.id, todo.text)}
                >
                  {todo.text}
                </span>
              )}
              <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
                {editingId !== todo.id && (
                  <button
                    onClick={() => startEdit(todo.id, todo.text)}
                    className="text-slate-500 hover:text-cyan-400"
                    aria-label="Edit to-do"
                  >
                    <Pencil className="w-3 h-3" />
                  </button>
                )}
                <button
                  onClick={() => removeFloatingTodo(todo.id)}
                  className="text-slate-500 hover:text-red-400"
                  aria-label="Remove to-do"
                >
                  <Trash2 className="w-3 h-3" />
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      {todos.length > 0 && (
        <div className="text-[10px] text-slate-500 px-1 mt-0.5">
          {todos.filter((t) => !t.done).length} open · {todos.filter((t) => t.done).length} done
        </div>
      )}
    </div>
  );
}
