'use client';

import { useMemo } from 'react';
import { ExternalLink, GraduationCap, RefreshCw } from 'lucide-react';
import { useVaultIndex } from '@/hooks/useVaultIndex';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Progress } from '@/components/ui/progress';
import { openNoteAt } from '@/lib/noteNavigation';

export function TreehouseView() {
  const { index, loading, error, refresh } = useVaultIndex();
  const derivedActivities = index.tasks.filter((task) => task.tags.some((tag) => tag.startsWith('course/') || tag.startsWith('skill/')));
  const skillGraph = useMemo(() => {
    const nodes = new Map(index.skills.map((skill) => [skill.label.toLowerCase(), { label: skill.label, status: skill.status, sourcePath: skill.sourcePath, evidence: skill.evidenceTasks.length, missing: false }]));
    for (const skill of index.skills) {
      for (const dep of skill.dependsOn) {
        const key = dep.toLowerCase();
        if (!nodes.has(key)) nodes.set(key, { label: dep, status: 'locked' as const, sourcePath: '', evidence: 0, missing: true });
      }
    }
    const list = [...nodes.values()];
    const positions = new Map(list.map((node, idx) => {
      const col = idx % 3;
      const row = Math.floor(idx / 3);
      return [node.label.toLowerCase(), { x: 90 + col * 170, y: 55 + row * 90 }];
    }));
    const edges = index.skills.flatMap((skill) => skill.dependsOn.map((dep) => ({ from: dep.toLowerCase(), to: skill.label.toLowerCase() })));
    return { nodes: list, positions, edges, height: Math.max(180, Math.ceil(Math.max(list.length, 1) / 3) * 90 + 40) };
  }, [index.skills]);

  return (
    <div className="w-full h-full rounded-xl border border-slate-700/50 bg-slate-950/40 overflow-hidden flex flex-col">
      <div className="px-3 py-2 border-b border-slate-700/50 bg-slate-900/40 flex items-center gap-2">
        <GraduationCap className="h-4 w-4 text-cyan-300" />
        <div className="text-xs font-semibold text-slate-200">Treehouse</div>
        <Badge variant="outline" className="ml-auto border-slate-600 text-slate-300 text-[10px]">
          {index.skills.length} skills · {index.courses.length} courses
        </Badge>
        <Button size="icon" variant="outline" className="h-7 w-7 border-slate-700 bg-slate-900" onClick={refresh}>
          <RefreshCw className="h-3.5 w-3.5" />
        </Button>
      </div>
      {error && <div className="m-3 text-xs text-red-200 border border-red-500/40 bg-red-950/30 rounded-md p-2">{error}</div>}
      {loading && <div className="p-4 text-xs text-slate-500">Loading learning graph...</div>}
      <div className="flex-1 min-h-0 overflow-y-auto p-3 grid grid-cols-1 xl:grid-cols-3 gap-3">
        <section className="rounded-md border border-slate-700/60 bg-slate-900/35 p-3 xl:col-span-2">
          <div className="text-xs font-semibold text-slate-100 mb-3">Skill tree</div>
          <svg viewBox={`0 0 560 ${skillGraph.height}`} className="w-full min-h-[220px] rounded-md border border-slate-800 bg-slate-950/40">
            {skillGraph.edges.map((edge) => {
              const from = skillGraph.positions.get(edge.from);
              const to = skillGraph.positions.get(edge.to);
              if (!from || !to) return null;
              return <line key={`${edge.from}-${edge.to}`} x1={from.x} y1={from.y} x2={to.x} y2={to.y} stroke="#0891b2" strokeOpacity="0.45" />;
            })}
            {skillGraph.nodes.map((node) => {
              const pos = skillGraph.positions.get(node.label.toLowerCase());
              if (!pos) return null;
              const color = node.missing ? '#f97316' : node.status === 'complete' ? '#22c55e' : '#22d3ee';
              return (
                <g key={node.label}>
                  <circle cx={pos.x} cy={pos.y} r="16" fill={color} fillOpacity="0.85" />
                  <text x={pos.x + 23} y={pos.y - 2} fill="#e2e8f0" fontSize="11">{node.label.slice(0, 30)}</text>
                  <text x={pos.x + 23} y={pos.y + 12} fill="#64748b" fontSize="9">{node.missing ? 'missing prerequisite' : `${node.status} · ${node.evidence} evidence`}</text>
                </g>
              );
            })}
          </svg>
        </section>

        <section className="rounded-md border border-slate-700/60 bg-slate-900/35 p-3">
          <div className="text-xs font-semibold text-slate-100 mb-3">Skills</div>
          <div className="space-y-2">
            {index.skills.map((skill) => (
              <div key={skill.id} className="border border-slate-800 rounded-md p-2">
                <div className="flex items-center gap-2">
                  <span className="text-xs text-slate-200">{skill.label}</span>
                  <Badge variant="outline" className="ml-auto text-[10px] border-cyan-800 text-cyan-200">{skill.status}</Badge>
                  <Button size="icon" variant="outline" className="h-6 w-6 border-slate-700 bg-slate-950" onClick={() => openNoteAt(skill.sourcePath, 1)} title="Open source">
                    <ExternalLink className="h-3 w-3" />
                  </Button>
                </div>
                <div className="text-[10px] text-slate-600 font-mono truncate mt-1">{skill.sourcePath}</div>
                <div className="text-[10px] text-emerald-400 mt-1">{skill.evidenceTasks.length} completed evidence task{skill.evidenceTasks.length === 1 ? '' : 's'}</div>
                {skill.dependsOn.length > 0 && <div className="text-[10px] text-slate-500 mt-1">needs {skill.dependsOn.join(', ')}</div>}
              </div>
            ))}
            {index.skills.length === 0 && <div className="text-[11px] text-slate-500">Add `skill:` frontmatter or `#skill/name` tags.</div>}
          </div>
        </section>
        <section className="rounded-md border border-slate-700/60 bg-slate-900/35 p-3">
          <div className="text-xs font-semibold text-slate-100 mb-3">Courses</div>
          <div className="space-y-2">
            {index.courses.map((course) => (
              <div key={course.id} className="border border-slate-800 rounded-md p-2">
                <div className="flex items-center gap-2">
                  <span className="text-xs text-slate-200">{course.label}</span>
                  <span className="ml-auto text-[10px] text-slate-500">{course.progress}%</span>
                  <Button size="icon" variant="outline" className="h-6 w-6 border-slate-700 bg-slate-950" onClick={() => openNoteAt(course.sourcePath, 1)} title="Open source">
                    <ExternalLink className="h-3 w-3" />
                  </Button>
                </div>
                <Progress value={course.progress} className="h-1.5 mt-2" />
                <div className="text-[10px] text-slate-600 font-mono truncate mt-1">{course.sourcePath}</div>
              </div>
            ))}
            {index.courses.length === 0 && <div className="text-[11px] text-slate-500">Add `course:` frontmatter or `#course/name` tags.</div>}
          </div>
        </section>
        <section className="rounded-md border border-slate-700/60 bg-slate-900/35 p-3">
          <div className="text-xs font-semibold text-slate-100 mb-3">Activities from tasks</div>
          <div className="space-y-1">
            {(derivedActivities.length ? derivedActivities : index.tasks).slice(0, 80).map((task) => (
              <div key={task.id} className="text-[11px] border-b border-slate-800 py-1">
                <div className={task.status === 'done' ? 'line-through text-slate-500' : 'text-slate-300'}>{task.title}</div>
                <button className="text-[10px] text-cyan-500" onClick={() => openNoteAt(task.sourcePath, task.line)}>{task.noteTitle}:L{task.line}</button>
              </div>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}
