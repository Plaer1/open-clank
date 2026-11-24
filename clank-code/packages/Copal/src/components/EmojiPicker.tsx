'use client';

import { useState, useMemo, useCallback, useEffect } from 'react';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import { EMOJI_CATEGORIES, ALL_EMOJI_SEARCH_INDEX, type EmojiEntry } from '@/lib/emoji-data';

const PAGE_SIZE = 80; // 10 rows × 8 cols
const FREQUENT_KEY = 'move-timeline-frequent-emojis';
const MAX_FREQUENT = 24;

function loadFrequent(): EmojiEntry[] {
  if (typeof window === 'undefined') return [];
  try {
    const raw = localStorage.getItem(FREQUENT_KEY);
    if (!raw) return [];
    const ids: string[] = JSON.parse(raw);
    return ids
      .map((char) => ALL_EMOJI_SEARCH_INDEX.find((e) => e.char === char))
      .filter(Boolean) as EmojiEntry[];
  } catch {
    return [];
  }
}

function saveFrequent(char: string) {
  if (typeof window === 'undefined') return;
  try {
    const raw = localStorage.getItem(FREQUENT_KEY);
    const ids: string[] = raw ? JSON.parse(raw) : [];
    const filtered = ids.filter((c) => c !== char);
    filtered.unshift(char);
    localStorage.setItem(FREQUENT_KEY, JSON.stringify(filtered.slice(0, MAX_FREQUENT)));
  } catch {
    // ignore
  }
}

interface EmojiPickerProps {
  value: string;
  onSelect: (emoji: string) => void;
}

export function EmojiPicker({ value, onSelect }: EmojiPickerProps) {
  const [activeCategory, setActiveCategory] = useState('smileys');
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(0);
  const [frequent, setFrequent] = useState<EmojiEntry[]>([]);

  // Load frequent on mount
  useEffect(() => {
    setFrequent(loadFrequent());
  }, []);

  const handleSelect = useCallback(
    (emoji: string) => {
      onSelect(emoji);
      saveFrequent(emoji);
      setFrequent(loadFrequent());
    },
    [onSelect],
  );

  // Search across all categories
  const searchResults = useMemo(() => {
    if (!search.trim()) return null;
    const q = search.toLowerCase().trim();
    return ALL_EMOJI_SEARCH_INDEX.filter(
      (e) =>
        e.name.toLowerCase().includes(q) ||
        e.keywords.some((k) => k.includes(q)),
    );
  }, [search]);

  // Active category data
  const categoryData = useMemo(() => {
    if (searchResults) return searchResults;
    if (activeCategory === 'frequent') return frequent;
    const cat = EMOJI_CATEGORIES.find((c) => c.id === activeCategory);
    return cat?.emojis ?? [];
  }, [activeCategory, searchResults, frequent]);

  const totalPages = Math.max(1, Math.ceil(categoryData.length / PAGE_SIZE));
  const safePage = Math.min(page, totalPages - 1);
  const pageEmojis = categoryData.slice(safePage * PAGE_SIZE, (safePage + 1) * PAGE_SIZE);

  // Reset page on category/search change
  useEffect(() => {
    setPage(0);
  }, [activeCategory, search]);

  return (
    <div className="flex flex-col gap-2">
      {/* Category tabs */}
      <div className="flex gap-0.5 overflow-x-auto pb-1 -mx-1 px-1">
        {EMOJI_CATEGORIES.map((cat) => {
          // Hide frequent tab if empty and not searching
          if (cat.id === 'frequent' && frequent.length === 0 && !search.trim()) return null;
          return (
            <button
              key={cat.id}
              type="button"
              onClick={() => {
                setActiveCategory(cat.id);
                setSearch('');
              }}
              title={cat.name}
              className={`shrink-0 w-8 h-8 rounded-md flex items-center justify-center text-base transition-colors ${
                activeCategory === cat.id && !search.trim()
                  ? 'bg-slate-700 text-white'
                  : 'text-slate-400 hover:bg-slate-800 hover:text-slate-200'
              }`}
            >
              {cat.icon}
            </button>
          );
        })}
      </div>

      {/* Search */}
      <Input
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        placeholder="Search emojis..."
        className="h-7 bg-slate-900 border-slate-700 text-xs"
      />

      {/* Category label + pagination */}
      <div className="flex items-center justify-between">
        <span className="text-[10px] text-slate-500 truncate">
          {search.trim()
            ? `${categoryData.length} result${categoryData.length !== 1 ? 's' : ''}`
            : EMOJI_CATEGORIES.find((c) => c.id === activeCategory)?.name ?? ''}
        </span>
        {totalPages > 1 && (
          <div className="flex items-center gap-1">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-5 w-5 p-0"
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              disabled={safePage === 0}
            >
              <ChevronLeft className="w-3 h-3" />
            </Button>
            <span className="text-[10px] text-slate-500 tabular-nums">
              {safePage + 1}/{totalPages}
            </span>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-5 w-5 p-0"
              onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
              disabled={safePage >= totalPages - 1}
            >
              <ChevronRight className="w-3 h-3" />
            </Button>
          </div>
        )}
      </div>

      {/* Emoji grid */}
      <div className="flex flex-wrap gap-0.5 max-h-48 overflow-y-auto p-1 rounded-md border border-slate-800 bg-slate-900/50">
        {pageEmojis.length === 0 ? (
          <span className="text-[10px] text-slate-500 p-2 w-full text-center">
            {search.trim() ? 'No matches found.' : 'No emojis in this category.'}
          </span>
        ) : (
          pageEmojis.map((entry) => (
            <button
              key={entry.char}
              type="button"
              onClick={() => handleSelect(entry.char)}
              title={entry.name}
              className={`w-7 h-7 rounded-md border text-base flex items-center justify-center shrink-0 transition-colors ${
                value === entry.char
                  ? 'border-cyan-400 bg-slate-700'
                  : 'border-transparent hover:bg-slate-800'
              }`}
            >
              {entry.char}
            </button>
          ))
        )}
      </div>
    </div>
  );
}
