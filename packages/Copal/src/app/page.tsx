'use client';

import { useEffect, useState, useRef } from 'react';
import { useMoveStore } from '@/store/useMoveStore';
import { useResolvedTracks } from '@/hooks/useResolvedTracks';
import { usePersistence } from '@/hooks/usePersistence';
import { EventEditorDialog } from '@/components/views/EventEditorDialog';
import { GalaxyView } from '@/components/views/GalaxyView';
import { TimelineView } from '@/components/views/TimelineView';
import { CalendarView } from '@/components/views/CalendarView';
import { TodoListView } from '@/components/views/TodoListView';
import { NotesView } from '@/components/views/NotesView';
import { VaultGraphView } from '@/components/views/VaultGraphView';
import { MindMapView } from '@/components/views/MindMapView';
import { BasesView } from '@/components/views/BasesView';
import { WikiView } from '@/components/views/WikiView';
import { TreehouseView } from '@/components/views/TreehouseView';
import { TrackTogglePanel } from '@/components/views/TrackTogglePanel';
import { TaskDetailSheet } from '@/components/views/TaskDetailSheet';
import { TagFilterPanel } from '@/components/views/TagFilterPanel';
import { FloatingTodosPanel } from '@/components/views/FloatingTodosPanel';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { Button } from '@/components/ui/button';
import {
  Blocks,
  Download,
  Calendar,
  Sparkles,
  Route,
  Plus,
  ListChecks,
  FileText,
  Network,
  GitFork,
  Table2,
  GraduationCap,
} from 'lucide-react';
import { resolveTracks } from '@/lib/hammock';
import { readNoteJumpFromHash } from '@/lib/noteNavigation';

const TAB_IDS = new Set(['galaxy', 'timeline', 'calendar', 'notes', 'graph', 'mind', 'bases', 'wiki', 'treehouse', 'todo']);
const PLANNING_TAB_IDS = new Set(['galaxy', 'timeline', 'calendar', 'todo']);

export default function Home() {
  const setSelectedTask = useMoveStore((s) => s.setSelectedTask);
  const data = useMoveStore((s) => s.data);
  const today = useMoveStore((s) => s.today);
  const { hammockStart } = useResolvedTracks();
  const [addOpen, setAddOpen] = useState(false);
  const [sidebarW, setSidebarW] = useState(280);
  const [dragging, setDragging] = useState(false);
  const [activeTab, setActiveTab] = useState('notes');
  const dragStart = useRef<{ x: number; w: number } | null>(null);

  useEffect(() => {
    function syncHash() {
      const params = new URLSearchParams(window.location.hash.replace(/^#/, ''));
      const tab = params.get('tab');
      if (tab && TAB_IDS.has(tab)) setActiveTab(tab);
      if (readNoteJumpFromHash(window.location.hash)) setActiveTab('notes');
    }
    syncHash();
    window.addEventListener('hashchange', syncHash);
    return () => window.removeEventListener('hashchange', syncHash);
  }, []);

  // Resizable splitter between sidebar and view panel.
  function onHandleDown(e: React.PointerEvent) {
    e.preventDefault();
    (e.currentTarget as Element).setPointerCapture(e.pointerId);
    dragStart.current = { x: e.clientX, w: sidebarW };
    setDragging(true);
  }
  function onHandleMove(e: React.PointerEvent) {
    const s = dragStart.current;
    if (!s) return;
    const dx = e.clientX - s.x;
    setSidebarW(Math.max(200, Math.min(560, s.w + dx)));
  }
  function onHandleUp(e: React.PointerEvent) {
    dragStart.current = null;
    setDragging(false);
    (e.currentTarget as Element).releasePointerCapture?.(e.pointerId);
  }

  // Load from / save to move-data.json via the Python server.
  usePersistence();

  function downloadJson() {
    // Generate an exportable JSON snapshot that an AI calendar manager can ingest.
    // Resolves AUTO startDates using the current hammock start.
    const resolvedTracks = resolveTracks(data.tracks, hammockStart);
    const exportPayload = {
      ...data,
      exportedAt: new Date().toISOString(),
      today,
      tracks: resolvedTracks.map((t) => ({
        ...t,
        tasks: t.tasks.map((task) => ({
          ...task,
          startDate:
            task.startDate === 'AUTO' ? hammockStart ?? task.startDate : task.startDate,
        })),
      })),
    };
    const blob = new Blob([JSON.stringify(exportPayload, null, 2)], {
      type: 'application/json',
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `move-timeline-${today}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }

  const planningOpen = PLANNING_TAB_IDS.has(activeTab);

  return (
    <div className="min-h-screen lg:h-screen lg:overflow-hidden flex flex-col bg-[#111318] text-slate-100">
      {/* Header */}
      <header className="shrink-0 border-b border-slate-800 bg-[#171a21] sticky top-0 z-40">
        <div className="px-3 sm:px-4 py-2 flex items-center justify-between flex-wrap gap-2">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-md border border-slate-700 bg-slate-900 flex items-center justify-center text-sm font-semibold text-cyan-200">
              C
            </div>
            <div>
              <h1 className="text-sm sm:text-base font-semibold tracking-tight">Copal</h1>
              <p className="text-[11px] text-slate-400">
                Local-first knowledge vault · Servo-only desktop target · Today:{' '}
                <span className="text-cyan-300 font-mono">{today}</span>
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <Button
              size="sm"
              onClick={() => setAddOpen(true)}
              className="bg-slate-800 hover:bg-slate-700 text-slate-100"
            >
              <Plus className="w-3.5 h-3.5 mr-1.5" />
              Add event
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={downloadJson}
              className="border-slate-600 text-slate-200 hover:bg-slate-800 hover:text-white"
            >
              <Download className="w-3.5 h-3.5 mr-1.5" />
              Export JSON
            </Button>
          </div>
        </div>
      </header>

      {/* Main content */}
      <main className="flex-1 min-h-0 w-full px-2 sm:px-3 py-3 flex flex-col gap-3">
        <div className="flex flex-col lg:flex-row gap-3 lg:gap-0 flex-1 min-h-0 lg:overflow-hidden">
          {/* Left sidebar (resizable on desktop) */}
          {planningOpen && (
            <aside
              className="flex w-full flex-col gap-3 lg:min-h-0 lg:overflow-y-auto lg:pr-1"
              style={{ width: `min(100%, ${sidebarW}px)` }}
            >
              <TrackTogglePanel />
              <TagFilterPanel />
              <FloatingTodosPanel />

              <div className="p-3 rounded-md border border-slate-800 bg-slate-950/40 text-[11px] text-slate-400 leading-relaxed">
                <div className="font-semibold text-slate-300 mb-1">Planning gravity</div>
                Move-planner views stay available here, but Copal opens as a vault workspace.
                Task gravity still uses <span className="text-cyan-300 font-mono">today</span> as center.

                <div className="mt-2 pt-2 border-t border-slate-800">
                  <div className="font-semibold text-slate-300 mb-1">Shared tasks</div>
                  Tasks can carry <code className="text-fuchsia-300">sharedTrackIds</code> and render across planning views.
                </div>

                <div className="mt-2 pt-2 border-t border-slate-800">
                  <div className="font-semibold text-slate-300 mb-1">Data format</div>
                  Planning data remains JSON-exportable for AI calendar tools.
                </div>
              </div>
            </aside>
          )}

          {/* Resize handle (desktop only) */}
          {planningOpen && (
            <div
              onPointerDown={onHandleDown}
              onPointerMove={onHandleMove}
              onPointerUp={onHandleUp}
              className={`hidden lg:block w-1.5 shrink-0 cursor-col-resize transition-colors ${
                dragging ? 'bg-cyan-500/60' : 'bg-slate-700/40 hover:bg-cyan-500/40'
              }`}
              title="Drag to resize"
            />
          )}

          {/* Right: views */}
          <section className="flex flex-col gap-2 min-h-0 lg:overflow-hidden flex-1 lg:pl-2">
            <Tabs value={activeTab} onValueChange={setActiveTab} className="flex-1 min-h-0 flex flex-col gap-3">
              <TabsList className="w-full max-w-full justify-start overflow-x-auto h-auto flex flex-nowrap bg-[#171a21] border border-slate-800 lg:w-auto">
                <TabsTrigger
                  value="galaxy"
                  className="data-[state=active]:bg-slate-800 data-[state=active]:text-cyan-300"
                >
                  <Sparkles className="w-3.5 h-3.5 mr-1.5" />
                  Galaxy
                </TabsTrigger>
                <TabsTrigger
                  value="timeline"
                  className="data-[state=active]:bg-slate-800 data-[state=active]:text-cyan-300"
                >
                  <Route className="w-3.5 h-3.5 mr-1.5" />
                  Timeline
                </TabsTrigger>
                <TabsTrigger
                  value="calendar"
                  className="data-[state=active]:bg-slate-800 data-[state=active]:text-cyan-300"
                >
                  <Calendar className="w-3.5 h-3.5 mr-1.5" />
                  Calendar
                </TabsTrigger>
                <TabsTrigger
                  value="notes"
                  className="data-[state=active]:bg-slate-800 data-[state=active]:text-cyan-300"
                >
                  <FileText className="w-3.5 h-3.5 mr-1.5" />
                  Notes
                </TabsTrigger>
                <TabsTrigger
                  value="graph"
                  className="data-[state=active]:bg-slate-800 data-[state=active]:text-cyan-300"
                >
                  <Network className="w-3.5 h-3.5 mr-1.5" />
                  Graph
                </TabsTrigger>
                <TabsTrigger
                  value="mind"
                  className="data-[state=active]:bg-slate-800 data-[state=active]:text-cyan-300"
                >
                  <GitFork className="w-3.5 h-3.5 mr-1.5" />
                  Mind
                </TabsTrigger>
                <TabsTrigger
                  value="bases"
                  className="data-[state=active]:bg-slate-800 data-[state=active]:text-cyan-300"
                >
                  <Table2 className="w-3.5 h-3.5 mr-1.5" />
                  Bases
                </TabsTrigger>
                <TabsTrigger
                  value="wiki"
                  className="data-[state=active]:bg-slate-800 data-[state=active]:text-cyan-300"
                >
                  <Blocks className="w-3.5 h-3.5 mr-1.5" />
                  Wiki
                </TabsTrigger>
                <TabsTrigger
                  value="treehouse"
                  className="data-[state=active]:bg-slate-800 data-[state=active]:text-cyan-300"
                >
                  <GraduationCap className="w-3.5 h-3.5 mr-1.5" />
                  Treehouse
                </TabsTrigger>
                <TabsTrigger
                  value="todo"
                  className="data-[state=active]:bg-slate-800 data-[state=active]:text-cyan-300"
                >
                  <ListChecks className="w-3.5 h-3.5 mr-1.5" />
                  To-do
                </TabsTrigger>
              </TabsList>

              <TabsContent
                value="galaxy"
                className="flex-1 min-h-[560px] lg:min-h-0 overflow-hidden data-[state=inactive]:hidden"
              >
                <GalaxyView onSelectTask={setSelectedTask} />
              </TabsContent>

              <TabsContent
                value="timeline"
                className="flex-1 min-h-[560px] lg:min-h-0 overflow-hidden data-[state=inactive]:hidden"
              >
                <TimelineView onSelectTask={setSelectedTask} />
              </TabsContent>

              <TabsContent
                value="calendar"
                className="flex-1 min-h-[560px] lg:min-h-0 overflow-hidden data-[state=inactive]:hidden"
              >
                <CalendarView onSelectTask={setSelectedTask} />
              </TabsContent>

              <TabsContent
                value="notes"
                className="flex-1 min-h-[560px] lg:min-h-0 overflow-hidden data-[state=inactive]:hidden"
              >
                <NotesView />
              </TabsContent>

              <TabsContent
                value="graph"
                className="flex-1 min-h-[560px] lg:min-h-0 overflow-hidden data-[state=inactive]:hidden"
              >
                <VaultGraphView />
              </TabsContent>

              <TabsContent
                value="mind"
                className="flex-1 min-h-[560px] lg:min-h-0 overflow-hidden data-[state=inactive]:hidden"
              >
                <MindMapView />
              </TabsContent>

              <TabsContent
                value="bases"
                className="flex-1 min-h-[560px] lg:min-h-0 overflow-hidden data-[state=inactive]:hidden"
              >
                <BasesView />
              </TabsContent>

              <TabsContent
                value="wiki"
                className="flex-1 min-h-[560px] lg:min-h-0 overflow-hidden data-[state=inactive]:hidden"
              >
                <WikiView />
              </TabsContent>

              <TabsContent
                value="treehouse"
                className="flex-1 min-h-[560px] lg:min-h-0 overflow-hidden data-[state=inactive]:hidden"
              >
                <TreehouseView />
              </TabsContent>

              <TabsContent
                value="todo"
                className="flex-1 min-h-[560px] lg:min-h-0 overflow-hidden data-[state=inactive]:hidden"
              >
                <TodoListView onSelectTask={setSelectedTask} />
              </TabsContent>
            </Tabs>
          </section>
        </div>
      </main>

      {/* Footer */}
      <footer className="shrink-0 border-t border-slate-800 bg-[#171a21] py-1.5 px-4 text-center text-[11px] text-slate-500">
        Copal vault workspace · Servo-only shell plan · Planning module: {data.tracks.length} tracks · JSON export available
      </footer>

      {/* Task detail sheet */}
      <TaskDetailSheet />

      {/* Add-event editor */}
      <EventEditorDialog open={addOpen} onOpenChange={setAddOpen} />
    </div>
  );
}
