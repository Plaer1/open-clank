// Seed data — mirrors /public/data/move-data.json so the app is self-contained
// on first paint, then fetches the JSON file on mount so AI calendar managers
// can edit it without touching the source.

import type { MoveData } from './types';

export const SEED: MoveData = {
  schemaVersion: 3,
  title: 'Ohio → Hawaii Move Master Timeline',
  originCity: 'Ohio',
  destinationCity: 'Hawaii',
  moveDeadline: '2026-09-01',
  globalStart: '2026-07-03',
  today: '2026-07-03',
  aiImportHints: {
    format:
      'Each track has a unique id, name, and tasks. Each task has id, title, description, startDate (YYYY-MM-DD | AUTO | FUZZY), dueDate (YYYY-MM-DD | null), status, priority, optional tags[], sharedTrackIds[], linkId, and fuzzy{anchorStart,anchorEnd,whiskerStart}. MoveData also has floatingTodos[] for track-less to-dos.',
    dateFormat: 'YYYY-MM-DD',
    specialTracks: {
      'relax-hammock': {
        autoStart: 'max(visibleTaskEffectiveEnd) + 1 day',
        extendsTo: 'infinity',
        renderHint: 'fade-out gradient',
      },
    },
    sharingV2: {
      taskFields: {
        tags: 'Optional string[]. Free-form tags for grouping/filtering.',
        sharedTrackIds:
          'Optional string[]. Additional track ids this task also belongs to. Renders as a hub node in Galaxy view and is deduplicated in Calendar view.',
        linkId:
          'Optional string. When two tasks on different tracks share the same linkId they are treated as the same shared event.',
      },
    },
    fuzzyV3: {
      taskFields: {
        fuzzy:
          'Optional {anchorStart?, anchorEnd?, whiskerStart?}. Used for tasks with uncertain dates. startDate="FUZZY" displays "?"; dueDate=null with fuzzy.anchorEnd set displays "?" end. Whisker zone is rendered as a box-and-whisker graphic.',
      },
      effectiveEnd: 'For hammock calc: dueDate ?? fuzzy.anchorEnd ?? null.',
      shrinkBehavior:
        'If today drifts past anchorStart, the hard portion of the chip shrinks (left edge slides to today) but the task is NOT greyed out — fuzzy tasks never display as "past/missed".',
    },
    floatingTodos: 'Optional array of {id, text, done, notes?}. Rendered in the sidebar; not on any timeline.',
  },
  floatingTodos: [
    {
      id: 'ft-fence-paperwork',
      text: 'make the fence paperwork',
      done: false,
    },
  ],
  tracks: [
    {
      id: 'eliott',
      name: 'Eliott',
      color: '#f97316',
      icon: 'clown',
      enabled: true,
      tasks: [
        {
          id: 'eliott-flight',
          title: 'Flight',
          description: 'Flight to Hawaii.',
          startDate: '2026-09-02',
          dueDate: '2026-09-02',
          status: 'pending',
          priority: 'high',
          tags: ['flight'],
        },
      ],
    },
    {
      id: 'pets-lcg',
      name: 'Leia, Chewie & Geener',
      color: '#ec4899',
      icon: 'cat',
      enabled: true,
      tasks: [
        {
          id: 'lcg-roadtrip',
          title: 'Road trip',
          description: 'Leia, Chewie & Geener road trip.',
          startDate: '2026-09-25',
          dueDate: '2026-10-05',
          status: 'pending',
          priority: 'high',
          tags: ['road-trip'],
        },
        {
          id: 'lcg-arrive-kona',
          title: 'Arrive in Kona',
          description: 'Arrive in Kona, Hawaii.',
          startDate: '2026-10-06',
          dueDate: '2026-10-06',
          status: 'pending',
          priority: 'high',
          tags: ['travel'],
        },
      ],
    },
    {
      id: 'doots',
      name: 'Doots',
      color: '#a855f7',
      icon: 'cat',
      enabled: true,
      tasks: [
        {
          id: 'doots-vet',
          title: 'Vet checkup & health certificate',
          description: 'Health cert for senior cat — full blood panel recommended.',
          startDate: '2026-07-05',
          dueDate: '2026-07-25',
          status: 'pending',
          priority: 'medium',
          tags: ['vet', 'health-cert', 'senior'],
        },
      ],
    },
    // ── U-Haul / U-Box (pack → transit → get the shit) ──
    {
      id: 'uhaul-ubox',
      name: 'U-Haul / U-Box',
      color: '#eab308',
      icon: 'truck',
      enabled: true,
      tasks: [
        {
          id: 'ucrate-pack',
          title: 'Pack U-Box',
          description: 'U-Box arrives at the house 7/30; picked up 8/2 (pack time).',
          startDate: '2026-07-30',
          dueDate: '2026-08-02',
          status: 'pending',
          priority: 'high',
          tags: ['packing', 'logistics'],
        },
        {
          id: 'ucrate-transit',
          title: 'U-Box in transit',
          description: 'Container in transit to Hawaii.',
          startDate: '2026-08-02',
          dueDate: '2026-09-18',
          status: 'pending',
          priority: 'medium',
          tags: ['shipping', 'logistics'],
        },
        {
          id: 'ucrate-get',
          title: 'Get the shit',
          description:
            'U-Box arrives — get our stuff. Fades out over ~a fortnight (no firm end date).',
          startDate: '2026-09-19',
          dueDate: null,
          status: 'ongoing',
          priority: 'medium',
          tags: ['unpacking', 'logistics'],
        },
      ],
    },
    // ── Get stuff from U-Haul (hidden by default) ────────────────────────
    {
      id: 'uhaul-pickup',
      name: 'Get stuff from U-Haul',
      color: '#92400e',
      icon: 'box',
      enabled: false,
      tasks: [
        {
          id: 'uhaul-pickup-hi',
          title: 'Pick up U-Box in Hawaii',
          description:
            'Container arrives at destination; schedule pickup or delivery window.',
          startDate: '2026-09-05',
          dueDate: '2026-09-15',
          status: 'pending',
          priority: 'medium',
          tags: ['logistics', 'unpacking'],
        },
      ],
    },
    {
      id: 'car',
      name: 'Car',
      color: '#0ea5e9',
      icon: 'car',
      enabled: true,
      tasks: [
        {
          id: 'car-ship',
          title: 'Ship car (pickup → Hilo)',
          description: 'Car picked up 7/16; arrives in Hilo 9/3.',
          startDate: '2026-07-16',
          dueDate: '2026-09-03',
          status: 'pending',
          priority: 'high',
          tags: ['shipping'],
        },
      ],
    },
    {
      id: 'termites',
      name: 'Termites',
      color: '#b45309',
      icon: 'bug',
      enabled: true,
      tasks: [
        {
          id: 'termites-inspect',
          title: 'Schedule termite inspection',
          description: 'Hire licensed inspector; required before sale/rental of OH property.',
          startDate: '2026-07-12',
          dueDate: '2026-08-01',
          status: 'pending',
          priority: 'medium',
          tags: ['inspection', 'pest'],
        },
        {
          id: 'termites-treat',
          title: 'Termite treatment (if needed)',
          description: 'Schedule treatment based on inspection findings.',
          startDate: '2026-08-02',
          dueDate: '2026-08-25',
          status: 'pending',
          priority: 'low',
          tags: ['treatment', 'pest'],
        },
      ],
    },
    {
      id: 'water-solar',
      name: 'Water & Solar',
      color: '#f59e0b',
      icon: 'sun',
      enabled: true,
      tasks: [
        {
          id: 'water-shutoff',
          title: 'Schedule water service shutoff',
          description: 'Notify City of OH water utility of final shutoff date.',
          startDate: '2026-08-15',
          dueDate: '2026-08-28',
          status: 'pending',
          priority: 'medium',
          tags: ['utility', 'shutoff'],
        },
        // ── Solar: fuzzy start (Sept 2), 3mo hard + 3mo whisker, fuzzy end ──
        {
          id: 'solar-decommission',
          title: 'Solar panel decommission/transfer',
          description:
            'Decide: buyout, transfer, or removal of solar system at OH property. Dates are nebulous for now — start is a placeholder (Sept 2, day after move deadline) and end is a placeholder 6 months out. The first 3 months are a normal hard task; the next 3 months are a whisker chart indicating uncertainty. The hammock line waits until this nebulous window closes. Replace FUZZY with a real start date when known — until then it will never display as "past/missed".',
          startDate: 'FUZZY',
          dueDate: null,
          status: 'ongoing',
          priority: 'medium',
          tags: ['solar', 'utility', 'fuzzy'],
          fuzzy: {
            anchorStart: '2026-09-02',
            whiskerStart: '2026-12-02',
            anchorEnd: '2027-03-02',
          },
        },
      ],
    },
    {
      id: 'fence',
      name: 'Fence',
      color: '#6b7280',
      icon: 'fence',
      enabled: true,
      tasks: [
        {
          id: 'fence-repair',
          title: 'Repair or replace backyard fence',
          description: 'Required for property sale/rental to pass inspection.',
          startDate: '2026-07-15',
          dueDate: '2026-08-18',
          status: 'pending',
          priority: 'low',
          tags: ['property', 'repair'],
        },
      ],
    },
    {
      id: 'ants',
      name: 'Ants',
      color: '#dc2626',
      icon: 'ant',
      enabled: true,
      tasks: [
        {
          id: 'ants-treat',
          title: 'Schedule ant extermination',
          description: 'Final pest treatment before move-out; ensure no infestation at handoff.',
          startDate: '2026-08-15',
          dueDate: '2026-08-28',
          status: 'pending',
          priority: 'low',
          tags: ['pest', 'treatment'],
        },
      ],
    },
    {
      id: 'toads',
      name: 'Toads',
      color: '#22c55e',
      icon: 'toad',
      enabled: true,
      tasks: [
        {
          id: 'toads-rehome',
          title: 'Rehome backyard toads',
          description: 'Find new home for resident toads before property handoff.',
          startDate: '2026-08-10',
          dueDate: '2026-08-29',
          status: 'pending',
          priority: 'low',
          tags: ['rehome', 'wildlife'],
        },
      ],
    },
    // ── Clean space for Geener — hard start Sept 2, fuzzy end 14 days out ──
    {
      id: 'geener-space',
      name: 'Clean space for Geener',
      color: '#10b981',
      icon: 'broom',
      enabled: true,
      tasks: [
        {
          id: 'geener-space-clean',
          title: 'Clean space for Geener in the house',
          description:
            'Make a clean, safe space for Geener in the new house. Start Sept 2 (day after move deadline), extend for a fortnight, end is fuzzy ("?") — no hard deadline. Replace the fuzzy end with a real date when known.',
          startDate: '2026-09-02',
          dueDate: null,
          status: 'ongoing',
          priority: 'medium',
          tags: ['cleaning', 'fuzzy'],
          fuzzy: {
            anchorEnd: '2026-09-16',
          },
        },
      ],
    },
    // ── Clean water tank — hard start Sept 2, fuzzy end 14 days out ──────
    {
      id: 'water-tank',
      name: 'Clean the poo poo out of the water tank',
      color: '#0891b2',
      icon: 'bucket',
      enabled: true,
      tasks: [
        {
          id: 'water-tank-clean',
          title: "Clean the poo poo out of the water tank",
          description:
            'Clean the water tank thoroughly. Start Sept 2 (day after move deadline), extend for a fortnight, end is fuzzy ("?") — no hard deadline. Replace the fuzzy end with a real date when known.',
          startDate: '2026-09-02',
          dueDate: null,
          status: 'ongoing',
          priority: 'medium',
          tags: ['cleaning', 'fuzzy'],
          fuzzy: {
            anchorEnd: '2026-09-16',
          },
        },
      ],
    },
    // ── Hammock (always last) ─────────────────────────────────────────────
    {
      id: 'relax-hammock',
      name: 'Relax on my hammock',
      color: '#06b6d4',
      icon: 'hammock',
      enabled: true,
      special: true,
      tasks: [
        {
          id: 'relax-hammock-day-1',
          title: 'Relax on my hammock',
          description:
            'Auto-generated: starts the day after the last visible task ends (including fuzzy-end tasks like Solar). Extends into the future and fades out so it looks like it never ends.',
          startDate: 'AUTO',
          dueDate: null,
          status: 'ongoing',
          priority: 'low',
          tags: ['relax'],
        },
      ],
    },
  ],
};
