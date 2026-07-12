# Worklog — Ohio → Hawaii Move Master Timeline

---
Task ID: 1
Agent: main
Task: Build an interactive modular move-planning web app with timeline, calendar, and galaxy gravity graph views. The "Relax on my hammock" line must auto-compute its start date as one day after the latest visible task due date, extending into the future with a fade-out effect. Provide per-track on/off toggles that recompute the hammock start. All data lives in a JSON file so an AI calendar manager can import it without reading the project source.

Work Log:
- Initialized fullstack dev environment (Next.js 16 + TypeScript + Tailwind + shadcn/ui).
- Created `/public/data/move-data.json` — schema-versioned, AI-friendly JSON with all 13 tracks (Eliott, Chewie, Leia, Doots, Geener, U-Haul/U-Box, Car, Termites, Water & Solar, Fence, Ants, Toads, Relax on my hammock) and an `aiImportHints` block explaining the special "AUTO" startDate semantics for the hammock.
- Built `src/lib/types.ts` for shared Track/Task/MoveData types.
- Built `src/store/useMoveStore.ts` (Zustand) with seed data, track toggles, and `getHammockStartDate()` selector that derives `max(visibleTaskDueDate) + 1 day`.
- Built `GalaxyView.tsx`: center = TODAY (glowing white-cyan core). Each task is a star; gravity (radius from center) is inversely proportional to |dueDate − today|. Each track is its own spiral arm using golden-angle + per-track arm offset. Past tasks fade and grey out. Hammock is a hollow cyan ring with a pulsing ping animation. Starfield background with twinkle animation.
- Built `TimelineView.tsx`: horizontal swimlane per track, 18px per day, month markers, weekly ticks, today marker, past-day greyed overlay, hammock lane rendered with a left-to-right fade gradient so it visually "never ends".
- Built `CalendarView.tsx`: 6-week month grid with prev/next nav, task chips colored by track (solid for due date, faint for start date), today ring, "🛶 start" badge on the auto-computed hammock start date.
- Built `TrackTogglePanel.tsx`: per-track switches, "All"/"None" bulk buttons, live "Hammock auto-update" readout showing latest visible due date and computed hammock start.
- Built `TaskDetailSheet.tsx`: right-side slide-out sheet with task details, badges, days-from-now countdown, and an explainer block for the hammock line.
- Wired `page.tsx` with header, sidebar (toggles + gravity explainer), tabbed views (Galaxy / Timeline / Calendar), JSON export button, and footer.
- Fixed infinite re-render bug: store selector functions (`getResolvedTracks`, `getHammockStartDate`, `getLatestVisibleDueDate`) returned new arrays/values each call — refactored all components to select raw `data` and compute resolved tracks + hammock start via `useMemo` with proper deps.
- Fixed Starfield hydration mismatch by gating `Math.random()` behind a `mounted` flag.
- Verified end-to-end with Agent Browser:
  - Page loads cleanly on `/`.
  - Galaxy view renders all 22 task nodes + today core + hammock ripple + starfield.
  - Timeline view shows all tracks with task chips; past-day greyed overlay works.
  - Calendar view shows July 2026; can navigate to August.
  - Task detail sheet opens when clicking a node.
  - **Hammock auto-recompute verified**: with all tracks on, latest due = 2026-08-31, hammock start = 2026-09-01. Toggled U-Haul off → latest due became 2026-08-30 (Drop off car at port), hammock start auto-updated to 2026-08-31 across all three views (panel readout, timeline title, galaxy tooltip). Re-enabled U-Haul → snapped back to 2026-09-01.
  - JSON export endpoint `/data/move-data.json` returns valid JSON.
- Lint passes (`bun run lint` — 0 errors).

Stage Summary:
- Final deliverable: interactive Next.js app at `/` with 3 views (Galaxy / Timeline / Calendar), per-track toggles, auto-recomputing hammock line, and JSON data file at `/public/data/move-data.json` for AI calendar managers.
- All requested features implemented: modular tracks (add more by editing JSON), per-track toggles, hammock starts day-after-last-visible-task and fades to infinity, gravity-based galaxy graph with today at center, greyed past days on timeline, JSON-exportable data.
- Screenshots saved to `/home/z/my-project/download/galaxy-view.png`, `timeline-view.png`, `calendar-view.png`, `calendar-august.png`.

---
Task ID: 2
Agent: main
Task: Add ability for some tasks to share timelines. E.g. if "Leia and Chewie" are going on the "same car ride" on the "same day", that should be a node that connects both timelines in Galaxy view. Don't recreate events in Calendar view (one chip, not duplicated). Add a tagging system.

Work Log:
- Extended `src/lib/types.ts`:
  - Added optional `tags?: string[]` field to `Task` (free-form tags for grouping/filtering).
  - Added optional `sharedTrackIds?: string[]` field — additional track IDs a task also belongs to (besides its parent track).
  - Added optional `linkId?: string` field — for linking two independently-defined tasks across tracks as the same shared event.
  - Added helper exports `getTaskTrackIds(task, parentTrackId)` (parent + shared, deduped) and `isSharedTask(task)`.
- Updated `public/data/move-data.json` (schemaVersion 2):
  - Added tags to every existing task (vet, health-cert, vaccines, quarantine, paperwork, flight, booking, logistics, packing, shipping, mechanic, prep, inspection, pest, treatment, utility, shutoff, solar, property, repair, rehome, wildlife, road-trip, shared, port-dropoff, car-ride, senior, relax).
  - Added two shared task examples:
    - `chewie-leia-carride` (2026-07-12): "Car ride to vet — Leia & Chewie together", defined on Chewie's track with `sharedTrackIds: ["leia"]`. Tags: vet, car-ride, shared.
    - `car-dropoff` (2026-08-22 → 2026-08-30): "Drop off car at port — whole family road trip", defined on the Car track with `sharedTrackIds: ["eliott","chewie","leia","doots","geener"]`. Tags: road-trip, shared, port-dropoff.
  - Updated `aiImportHints` with a new `sharingV2` block documenting the three new task fields with an example.
- Updated `src/store/useMoveStore.ts`:
  - Mirrored the seed data (same shared tasks + tags).
  - Added `activeTagFilter` state + `toggleTagFilter`/`clearTagFilters` actions.
  - Added `TaskWithTrack` interface (task + parentTrack + allTrackIds + isShared).
  - Added selectors: `getAllTags()`, `getAllTasksFlattened()`, `getTasksForTrack(trackId)`, `passesTagFilter(task)`.
  - Tag filter uses AND semantics: a task passes only if it has ALL selected tags.
- Rewrote `src/components/views/GalaxyView.tsx`:
  - Shared tasks now render as hub nodes positioned at the angular midpoint of all their visible track arms (averaged x/y of each arm endpoint). They get a slightly larger radius and a multi-color conic-gradient core built from all their track colors.
  - Added dashed outer ring around shared hubs for emphasis.
  - Added SVG `<line>` edges from each shared hub to each non-parent track arm endpoint, colored per target track, dashed, plus a small target ring at the endpoint to anchor the visual.
  - Replaced the per-task emoji on shared hubs with a 🔗 glyph.
  - Added a legend entry "Shared hub (multi-track event)" with a conic-gradient swatch.
  - Added tag-filter subscription so the galaxy re-renders when filter changes (skips filtered-out tasks).
- Rewrote `src/components/views/TimelineView.tsx`:
  - Each track's swimlane now renders every task whose `getTaskTrackIds` includes that track's id — so shared tasks appear on every track they belong to.
  - Shared task chips use the current lane's color for the body (so they visually belong to that swimlane) but get: a dashed border, a multi-color top stripe gradient (all parent/shared track colors), a 🔗 prefix icon, and a `{N} tracks` suffix on the date line.
  - Tooltip lists all tracks the task is shared across.
  - Tag-filter applied at the per-track task-list level.
- Rewrote `src/components/views/CalendarView.tsx`:
  - Shared tasks are deduplicated by task ID — a task with `sharedTrackIds` appears as ONE chip on its due date (and once on its start date if different), not once per track.
  - Shared chip on the due date uses a multi-color horizontal gradient background (cycling through all track colors). On non-due dates (start marker), uses a striped repeating gradient.
  - White border on shared chips + box-shadow ring for visibility.
  - Each shared chip shows a row of small color dots (mini legend) for up to 5 tracks; "+N" for more.
  - Tag-filter applied at the date-map build time (filtered-out tasks never enter the map).
  - Added a compact legend bar at the bottom of the calendar (today / start / due / shared).
- Updated `src/components/views/TaskDetailSheet.tsx`:
  - Header now shows a row of colored dots — one per track the task belongs to (parent outlined in white).
  - Added "🔗 shared hub" badge for shared tasks.
  - Added "Shared across" panel listing every track the task belongs to with a colored pill per track, "(defines)" marker on the parent, and an explainer paragraph.
  - Added "Tags" section showing all tags as `#tag` mono badges.
  - Added explainer block for shared tasks (mirrors the hammock explainer pattern).
- Created `src/components/views/TagFilterPanel.tsx`:
  - Lists every tag in the data with a count badge (e.g. `#vet ×7`).
  - Click toggles tag in/out of `activeTagFilter`. Active tags highlighted in cyan.
  - "Clear" button when filter is active.
  - Empty filter → panel hides entirely (clean sidebar).
- Updated `src/app/page.tsx`:
  - Imported and placed `<TagFilterPanel />` between `<TrackTogglePanel />` and the explainer block in the sidebar.
  - Added a "🔗 Shared tasks (new)" section to the explainer block documenting the new sharedTrackIds/tags fields and how each view renders them.
  - Updated footer copy to mention shared tasks + tags.
- ESLint config: added `react-hooks/set-state-in-effect: off` (shadcn/ui scaffolding + Starfield mount detection pattern) and added `upload/`, `download/` to ignores. Removed `extracted/` artifact dir.
- Lint passes clean (`bun run lint` — 0 errors, 0 warnings).

Stage Summary:
- Final deliverable: same Next.js app at `/` with 3 views, now with multi-track shared tasks + a tag system.
- New schema fields: `tags`, `sharedTrackIds`, `linkId` (all optional, backward-compatible — old data without these fields still works).
- New seed examples: "Car ride to vet — Leia & Chewie together" (2 tracks) and "Drop off car at port — whole family road trip" (6 tracks: Car + 5 pets).
- Galaxy: shared tasks = multi-color hub nodes with dashed edges to each track arm.
- Timeline: shared tasks = chips on every track they belong to, dashed border + multi-color top stripe.
- Calendar: shared tasks = ONE chip per date with multi-color gradient background + color-dot legend. No duplication.
- TaskDetailSheet: shows shared-across pills + tag chips + explainer.
- Sidebar: new TagFilterPanel for filtering by tag (AND semantics) across all three views.
- Lint clean; verified next step is dev server boot + Agent Browser smoke test.

Verification (Agent Browser, end-to-end):
- Page loads at the preview URL (https://preview-chat-c5ae1e34-7565-4d8f-88d0-f0b0527ac97b.space-z.ai/), title = "Ohio → Hawaii Move · Master Timeline", HTTP 200, no console errors.
- Galaxy view: 24 task nodes render (22 original + 2 new shared). The 2 shared tasks ("Car ride to vet — Leia & Chewie together" and "Drop off car at port — whole family road trip") render as 🔗 hub nodes with multi-color conic-gradient cores and dashed edges to each non-parent track arm. Legend includes "= Shared hub (multi-track event)".
- Clicked 🔗 hub → TaskDetailSheet opens with "🔗 shared hub" badge, "SHARED ACROSS" panel showing colored pills for Chewie (defines) + Leia, "Tags" section showing #vet #car-ride #shared, plus the shared-task explainer block.
- Timeline view: "Car ride to vet" chip appears on 2 swimlanes (Chewie + Leia) with dashed border + multi-color top stripe + 🔗 prefix + "2 tracks" suffix. "Drop off car at port" chip appears on 6 swimlanes (Car + 5 pets). Both have "Shared across: ..." tooltips.
- Calendar view (July): "Car ride to vet" appears as ONE chip on July 12 with "Chewie + 1 more" prefix and color-dot legend. NOT duplicated.
- Calendar view (August): "Drop off car at port" appears as exactly TWO chips (Aug 22 start + Aug 30 due), each with "Car + 5 more" prefix. NOT 6 chips per date.
- Tag filter (#shared active): Galaxy shows only the 2 shared hubs; Calendar July shows only the 1 shared chip; non-shared "Vet checkup" tasks correctly filtered out (0 results). Tooltip changes to "Click to remove filter: #shared" when active.
- Hammock auto-recompute regression check: U-Haul ON → latest=2026-08-31, hammock=2026-09-01. U-Haul OFF → latest=2026-08-30 (the shared "Drop off car at port" task), hammock=2026-08-31. U-Haul ON → snaps back to 2026-09-01. Shared task dueDates correctly participate in the hammock calculation.
- Lint passes clean (0 errors, 0 warnings).
- Screenshots saved: galaxy-view-shared.png, galaxy-shared-detail.png, timeline-view-shared.png, calendar-view-shared.png, calendar-august-shared.png, calendar-july-tag-filter-shared.png, galaxy-tag-filter-shared.png.

---
Task ID: 3
Agent: main
Task: Add fuzzy/nebulous Solar task (3mo hard + 3mo whisker, fuzzy start/end, hammock waits for it). Split uhaul-ucrate into packing + unpacking. Add hidden "get stuff from uhaul" track. Add "clean space for geener" + "clean water tank" tracks with hard start + fuzzy 14-day end. Add floating todos section (with "make the fence paperwork"). Audit code base — modularize, extract helpers, eliminate redundancy. Zip project.

Work Log:
- **Refactor — extracted lib layer:**
  - `src/lib/dates.ts` — parseDate, fmtDate, addDays, addMonths, daysBetween, isPastDay, fmtShortLabel. (Was in store, re-exported.)
  - `src/lib/sharing.ts` — getTaskTrackIds, isSharedTask. (Was in types.ts; types.ts is now type-only.)
  - `src/lib/fuzzy.ts` — FuzzySpec type, isFuzzyStart, isFuzzyEnd, hasWhisker, getLayoutStart, getLayoutEnd, getWhiskerStart, getHardPortionStart, displayStart, displayEnd, getEffectiveEnd. All the new fuzzy logic in one place.
  - `src/lib/hammock.ts` — computeLatestVisibleEnd, computeHammockStart, resolveTracks, getLatestVisibleEndStr. (Was duplicated 3x: store + TrackTogglePanel + TaskDetailSheet.)
  - `src/lib/render.ts` — buildConicGradient, buildStripeGradient, buildSharedChipBackground, colorsForTrackIds, buildTaskRenderSpec, shouldGreyOut. Pure rendering helpers.
  - `src/lib/seed.ts` — SEED data extracted to its own file (was 100+ lines inline in store).
  - `src/lib/types.ts` — now type-only + ICON_MAP. Added FuzzySpec re-export, FloatingTodo interface, TrackColorLookup type. Added broom/bucket/box icons.

- **Refactor — extracted hooks:**
  - `src/hooks/useResolvedTracks.ts` — returns {resolvedTracks, trackById, hammockStart, latestVisibleEnd, latestVisibleEndStr} as one memoized bundle. Was copy-pasted IDENTICALLY in 4 views (Galaxy/Timeline/Calendar/TaskDetailSheet).
  - `src/hooks/useTagFilter.ts` — returns {active, isActive, passes, toggle, clear}. Wraps the store's tag-filter state so views don't each subscribe to activeTagFilter + passesTagFilter separately.

- **Refactor — store slimmed:**
  - `src/store/useMoveStore.ts` went from 358 lines to ~210 lines. All calculation logic delegated to lib/hammock. Removed getHammockStartDate/getResolvedTracks/getLatestVisibleDueDate/getAllTasksFlattened/getTasksForTrack selectors — components now use the hooks + flatMapTasks helper. Added floatingTodos actions (addFloatingTodo, toggleFloatingTodo, removeFloatingTodo). Re-exports parseDate/fmtDate from lib/dates for backward compat.

- **New data model — fuzzy tasks:**
  - Task.fuzzy?: { anchorStart?, anchorEnd?, whiskerStart? }
  - startDate='FUZZY' → display '?', lay out from anchorStart, NEVER grey out (the "no feeling of failure" requirement).
  - dueDate=null + fuzzy.anchorEnd set → display '?', extend chip to anchorEnd.
  - fuzzy.whiskerStart set → chip transitions from solid to box-and-whisker graphic at this date.
  - "Shrink" behavior: getHardPortionStart() returns max(anchorStart, today) for fuzzy-start tasks — the hard portion's left edge slides forward as today advances, so the visible "certain" time shrinks. Implemented as a sub-overlay in the timeline chip.

- **New data model — floatingTodos:**
  - MoveData.floatingTodos?: FloatingTodo[] = { id, text, done, notes? }[]
  - Rendered in the new FloatingTodosPanel sidebar component.
  - Seed includes "make the fence paperwork".

- **Updated Solar task (water-solar track):**
  - Was: startDate=2026-07-20, dueDate=2026-08-20 (hard)
  - Now: startDate='FUZZY', dueDate=null, fuzzy={anchorStart:'2026-09-02', whiskerStart:'2026-12-02', anchorEnd:'2027-03-02'}
  - Description rewritten to explain the 3mo-hard + 3mo-whisker semantics and the "no feeling of failure" promise.
  - Tags: ['solar','utility','fuzzy'].

- **Hammock calc updated:**
  - computeLatestVisibleEnd() now uses getEffectiveEnd(task) = dueDate ?? fuzzy.anchorEnd ?? null.
  - Verified: Water & Solar ON → latest end = 2027-03-02 (Solar's anchorEnd), hammock = 2027-03-03.
  - Verified: Water & Solar OFF → latest end = 2026-09-16 (cleaning tasks' anchorEnd), hammock = 2026-09-17.
  - Hammock correctly waits for the 6-month nebulous Solar window to close before starting.

- **Split uhaul-ucrate into two tracks:**
  - "U-Haul (packing)" track: ucrate-reserve (Jul 8-20) + ucrate-pack (Jul 21-Aug 4). Yellow #eab308.
  - "U-Haul (unpacking)" track: ucrate-ship (Aug 5-20) + ucrate-unpack (Aug 21-31). Brown #a16207.
  - Original date range (Jul 8 → Aug 31) split approximately down the middle (Aug 4/Aug 5).

- **Added "Get stuff from U-Haul" track:**
  - enabled:false (hidden by default — verified aria-checked="false").
  - Color #92400e, icon 'box'.
  - Task: uhaul-pickup-hi (Sep 5-15, 2026).

- **Added "Clean space for Geener" track:**
  - startDate=2026-09-02, dueDate=null, fuzzy={anchorEnd:'2026-09-16'} (14 days out).
  - Color #10b981, icon 'broom'. Tagged ['cleaning','fuzzy'].

- **Added "Clean the poo poo out of the water tank" track:**
  - startDate=2026-09-02, dueDate=null, fuzzy={anchorEnd:'2026-09-16'} (14 days out).
  - Color #0891b2, icon 'bucket'. Tagged ['cleaning','fuzzy'].

- **View updates — fuzzy rendering:**
  - GalaxyView: fuzzy nodes render as dashed purple ring with "?" in center. Fuzzy-start tasks NEVER grey out. Whisker-zone tasks get a faint dashed arc from start anchor to end anchor with a dashed target ring at the end. Legend updated with "Fuzzy / nebulous (?)" row.
  - TimelineView: fuzzy chips get a "?" marker on the left (fuzzy start) and/or right (fuzzy end) edge. Whisker-zone chips render a box-and-whisker SVG overlay (top/bottom whisker lines + IQR box + median tick) over the second half of the chip. Background gradient transitions from solid (hard) to faint (whisker). Fuzzy-start chips use the "hard portion slides forward" behavior via getHardPortionStart.
  - CalendarView: fuzzy-start tasks show a chip with "?" prefix on the anchorStart date (dashed border). Fuzzy-end tasks show a chip with "?" prefix on the anchorEnd date (dashed border). Both display "fuzzy start"/"fuzzy end" in the tooltip. Legend updated with "?" row.
  - TaskDetailSheet: shows "❓ fuzzy dates" + "whisker zone" badges. Start/End display "?" with anchor info in italic. Whisker-zone-starts row added. Dedicated "❓ About fuzzy dates" explainer block in purple.

- **FloatingTodosPanel:**
  - New sidebar component. Lists todos with checkboxes. Add new via input + Enter. Remove via hover trash icon. Shows open/done count.
  - Seed includes "make the fence paperwork".
  - Verified: added "test from agent browser" via UI, confirmed it appeared in list, removed it via trash icon, confirmed removal.

- **JSON seed (`/public/data/move-data.json`) updated to schemaVersion 3:**
  - aiImportHints now includes a fuzzyV3 block documenting the new task.fuzzy field, effectiveEnd semantics, and shrinkBehavior.
  - aiImportHints.floatingTodos documents the new top-level field.
  - All 17 tracks + 1 floatingTodo present.

- **Page.tsx:**
  - Uses useResolvedTracks() hook for hammockStart instead of useMoveStore.getState().getHammockStartDate() (cleaner, reactive).
  - Export JSON uses resolveTracks() from lib/hammock instead of the old store selector.
  - Sidebar now includes FloatingTodosPanel between TagFilterPanel and the explainer.
  - Explainer block updated with new "🔗 Shared tasks" + "❓ Fuzzy tasks" sections.
  - Footer mentions sharedTrackIds + fuzzy.

- **Verification (Agent Browser, end-to-end):**
  - Page loads, title = "Ohio → Hawaii Move · Master Timeline", HTTP 200, no console errors.
  - 17 tracks listed in toggle panel; "Get stuff from U-Haul" is hidden by default (aria-checked="false"). All other 16 are on.
  - Hammock auto-update readout: "Latest visible end: 2027-03-02 / Hammock starts: 2027-03-03" — confirms the Solar fuzzy task's anchorEnd correctly pushes the hammock out by 6 months.
  - Toggling Water & Solar off → latest end becomes 2026-09-16 (cleaning tasks' fuzzy end), hammock → 2026-09-17. Toggling back on → snaps back to 2027-03-03.
  - Galaxy view: 3 fuzzy nodes render as dashed purple rings with "?" in center. Whisker-zone arc visible from Solar's Sept 2 anchor to its March 2 anchor.
  - Timeline view: fuzzy chips have "?" markers on left/right edges. Solar chip shows box-and-whisker graphic overlay in the second half. Cleaning task chips extend 14 days from Sept 2 with fade-to-? at the end.
  - Calendar view (Sept 2026): Solar chip on Sept 2 with "(fuzzy start)" tooltip. Both cleaning tasks on Sept 16 with "(fuzzy end)" tooltip.
  - Calendar view (March 2027): Solar chip on March 2 with "(fuzzy end)" tooltip. Hammock "🛶 start" on March 3 — confirms hammock waits for Solar's nebulous window to close.
  - TaskDetailSheet (clicked Solar fuzzy node): shows "❓ fuzzy dates" + "whisker zone" badges. Start: "? (fuzzy — anchor 2026-09-02)". End: "? (fuzzy — anchor 2027-03-02)". Whisker zone starts: "2026-12-02". "❓ About fuzzy dates" explainer present. Tags: #solar #utility #fuzzy.
  - FloatingTodosPanel: "make the fence paperwork" present. Add/toggle/remove all work.
  - JSON endpoint `/data/move-data.json` returns valid schemaVersion=3, 17 tracks, 1 floatingTodo, hasFuzzy=true.
  - Lint passes clean (0 errors, 0 warnings).
  - Screenshots saved: v3-galaxy.png, v3-timeline.png, v3-calendar-sept.png, v3-calendar-march-2027.png, v3-solar-detail.png.

Stage Summary:
- 5 new tracks added (U-Haul packing, U-Haul unpacking, Get stuff from U-Haul [hidden], Clean space for Geener, Clean water tank) → 17 total.
- Solar task converted to fuzzy with 3mo hard + 3mo whisker + fuzzy start/end. Hammock correctly waits for it.
- 2 cleaning tasks with hard Sept 2 start + 14-day fuzzy end ("?" instead of dot, no hard end).
- FloatingTodosPanel added with "make the fence paperwork" seeded. Add/toggle/remove all functional.
- Major refactor: extracted dates.ts, sharing.ts, fuzzy.ts, hammock.ts, render.ts, seed.ts lib modules. Extracted useResolvedTracks + useTagFilter hooks. Eliminated 4x duplicate resolvedTracks useMemo, 3x duplicate hammock calc, 4x duplicate trackById Map. Store went from 358 → ~210 lines. All views slimmer.
- Lint clean. Agent Browser verification passed across all 3 views + detail sheet + sidebar.
