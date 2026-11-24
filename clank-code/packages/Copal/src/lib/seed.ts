// Public synthetic default data; no personal planning payload is bundled.
// A local mutable /data/move-data.json can override it at runtime and stays private.

import type { MoveData } from './types';

export const SEED: MoveData = {
  "schemaVersion": 3,
  "title": "Example planning workspace",
  "originCity": "Example origin",
  "destinationCity": "Example destination",
  "moveDeadline": "2030-02-15",
  "globalStart": "2030-01-01",
  "today": "2030-01-01",
  "aiImportHints": {
    "format": "Synthetic planning example; AUTO begins after the latest enabled task deadline.",
    "dateFormat": "YYYY-MM-DD",
    "specialTracks": {
      "relax-hammock": {
        "autoStart": "Synthetic planning example; AUTO begins after the latest enabled task deadline.",
        "extendsTo": "Synthetic planning example; AUTO begins after the latest enabled task deadline.",
        "renderHint": "Synthetic planning example; AUTO begins after the latest enabled task deadline."
      }
    },
    "sharingV2": {
      "taskFields": {
        "tags": "Synthetic planning example; AUTO begins after the latest enabled task deadline.",
        "sharedTrackIds": "Synthetic planning example; AUTO begins after the latest enabled task deadline.",
        "linkId": "Synthetic planning example; AUTO begins after the latest enabled task deadline."
      }
    },
    "fuzzyV3": {
      "taskFields": {
        "fuzzy": "Synthetic planning example; AUTO begins after the latest enabled task deadline."
      },
      "effectiveEnd": "Synthetic planning example; AUTO begins after the latest enabled task deadline.",
      "shrinkBehavior": "Synthetic planning example; AUTO begins after the latest enabled task deadline."
    },
    "floatingTodos": "Synthetic planning example; AUTO begins after the latest enabled task deadline."
  },
  "floatingTodos": [
    {
      "id": "todo-01",
      "text": "Example undated task 1",
      "done": false
    }
  ],
  "tracks": [
    {
      "id": "track-01",
      "name": "Example track 1",
      "color": "#f97316",
      "icon": "clown",
      "enabled": true,
      "tasks": [
        {
          "id": "task-01",
          "title": "Example task 1.1",
          "description": "Synthetic planning example",
          "startDate": "2030-02-18",
          "dueDate": "2030-02-18",
          "status": "pending",
          "priority": "high",
          "tags": [
            "example"
          ]
        }
      ]
    },
    {
      "id": "track-02",
      "name": "Example track 2",
      "color": "#ec4899",
      "icon": "cat",
      "enabled": true,
      "tasks": [
        {
          "id": "task-02",
          "title": "Example task 2.1",
          "description": "Synthetic planning example",
          "startDate": "2030-03-11",
          "dueDate": "2030-03-14",
          "status": "pending",
          "priority": "high",
          "tags": [
            "example"
          ]
        },
        {
          "id": "task-03",
          "title": "Example task 2.2",
          "description": "Synthetic planning example",
          "startDate": "2030-03-17",
          "dueDate": "2030-03-17",
          "status": "pending",
          "priority": "high",
          "tags": [
            "example"
          ]
        }
      ]
    },
    {
      "id": "track-03",
      "name": "Example track 3",
      "color": "#a855f7",
      "icon": "cat",
      "enabled": true,
      "tasks": [
        {
          "id": "task-04",
          "title": "Example task 3.1",
          "description": "Synthetic planning example",
          "startDate": "2030-01-04",
          "dueDate": "2030-01-16",
          "status": "pending",
          "priority": "medium",
          "tags": [
            "example"
          ]
        }
      ]
    },
    {
      "id": "track-04",
      "name": "Example track 4",
      "color": "#eab308",
      "icon": "truck",
      "enabled": true,
      "tasks": [
        {
          "id": "task-05",
          "title": "Example task 4.1",
          "description": "Synthetic planning example",
          "startDate": "2030-01-19",
          "dueDate": "2030-01-25",
          "status": "pending",
          "priority": "high",
          "tags": [
            "example"
          ]
        },
        {
          "id": "task-06",
          "title": "Example task 4.2",
          "description": "Synthetic planning example",
          "startDate": "2030-01-25",
          "dueDate": "2030-03-05",
          "status": "pending",
          "priority": "medium",
          "tags": [
            "example"
          ]
        },
        {
          "id": "task-07",
          "title": "Example task 4.3",
          "description": "Synthetic planning example",
          "startDate": "2030-03-08",
          "dueDate": null,
          "status": "ongoing",
          "priority": "medium",
          "tags": [
            "example"
          ]
        }
      ]
    },
    {
      "id": "track-05",
      "name": "Example track 5",
      "color": "#92400e",
      "icon": "box",
      "enabled": false,
      "tasks": [
        {
          "id": "task-08",
          "title": "Example task 5.1",
          "description": "Synthetic planning example",
          "startDate": "2030-02-24",
          "dueDate": "2030-02-27",
          "status": "pending",
          "priority": "medium",
          "tags": [
            "example"
          ]
        }
      ]
    },
    {
      "id": "track-06",
      "name": "Example track 6",
      "color": "#0ea5e9",
      "icon": "track-06",
      "enabled": true,
      "tasks": [
        {
          "id": "task-09",
          "title": "Example task 6.1",
          "description": "Synthetic planning example",
          "startDate": "2030-01-13",
          "dueDate": "2030-02-21",
          "status": "pending",
          "priority": "high",
          "tags": [
            "example"
          ]
        }
      ]
    },
    {
      "id": "track-07",
      "name": "Example track 7",
      "color": "#b45309",
      "icon": "bug",
      "enabled": true,
      "tasks": [
        {
          "id": "task-10",
          "title": "Example task 7.1",
          "description": "Synthetic planning example",
          "startDate": "2030-01-07",
          "dueDate": "2030-01-22",
          "status": "pending",
          "priority": "medium",
          "tags": [
            "example"
          ]
        },
        {
          "id": "task-11",
          "title": "Example task 7.2",
          "description": "Synthetic planning example",
          "startDate": "2030-01-25",
          "dueDate": "2030-02-06",
          "status": "pending",
          "priority": "low",
          "tags": [
            "example"
          ]
        }
      ]
    },
    {
      "id": "track-08",
      "name": "Example track 8",
      "color": "#f59e0b",
      "icon": "sun",
      "enabled": true,
      "tasks": [
        {
          "id": "task-12",
          "title": "Example task 8.1",
          "description": "Synthetic planning example",
          "startDate": "2030-01-31",
          "dueDate": "2030-02-09",
          "status": "pending",
          "priority": "medium",
          "tags": [
            "example"
          ]
        },
        {
          "id": "task-13",
          "title": "Example task 8.2",
          "description": "Synthetic planning example",
          "startDate": "FUZZY",
          "dueDate": null,
          "status": "ongoing",
          "priority": "medium",
          "tags": [
            "example"
          ],
          "fuzzy": {
            "anchorStart": "2030-02-18",
            "whiskerStart": "2030-03-20",
            "anchorEnd": "2030-03-23"
          }
        }
      ]
    },
    {
      "id": "track-09",
      "name": "Example track 9",
      "color": "#6b7280",
      "icon": "track-09",
      "enabled": true,
      "tasks": [
        {
          "id": "task-14",
          "title": "Example task 9.1",
          "description": "Synthetic planning example",
          "startDate": "2030-01-10",
          "dueDate": "2030-02-03",
          "status": "pending",
          "priority": "low",
          "tags": [
            "example"
          ]
        }
      ]
    },
    {
      "id": "track-10",
      "name": "Example track 10",
      "color": "#dc2626",
      "icon": "ant",
      "enabled": true,
      "tasks": [
        {
          "id": "task-15",
          "title": "Example task 10.1",
          "description": "Synthetic planning example",
          "startDate": "2030-01-31",
          "dueDate": "2030-02-09",
          "status": "pending",
          "priority": "low",
          "tags": [
            "example"
          ]
        }
      ]
    },
    {
      "id": "track-11",
      "name": "Example track 11",
      "color": "#22c55e",
      "icon": "toad",
      "enabled": true,
      "tasks": [
        {
          "id": "task-16",
          "title": "Example task 11.1",
          "description": "Synthetic planning example",
          "startDate": "2030-01-28",
          "dueDate": "2030-02-12",
          "status": "pending",
          "priority": "low",
          "tags": [
            "example"
          ]
        }
      ]
    },
    {
      "id": "track-12",
      "name": "Example track 12",
      "color": "#10b981",
      "icon": "broom",
      "enabled": true,
      "tasks": [
        {
          "id": "task-17",
          "title": "Example task 12.1",
          "description": "Synthetic planning example",
          "startDate": "2030-02-18",
          "dueDate": null,
          "status": "ongoing",
          "priority": "medium",
          "tags": [
            "example"
          ],
          "fuzzy": {
            "anchorEnd": "2030-03-02"
          }
        }
      ]
    },
    {
      "id": "track-13",
      "name": "Example track 13",
      "color": "#0891b2",
      "icon": "bucket",
      "enabled": true,
      "tasks": [
        {
          "id": "task-18",
          "title": "Example task 13.1",
          "description": "Synthetic planning example",
          "startDate": "2030-02-18",
          "dueDate": null,
          "status": "ongoing",
          "priority": "medium",
          "tags": [
            "example"
          ],
          "fuzzy": {
            "anchorEnd": "2030-03-02"
          }
        }
      ]
    },
    {
      "id": "relax-hammock",
      "name": "Open-ended example",
      "color": "#06b6d4",
      "icon": "hammock",
      "enabled": true,
      "special": true,
      "tasks": [
        {
          "id": "task-19",
          "title": "Example task 14.1",
          "description": "Synthetic planning example",
          "startDate": "AUTO",
          "dueDate": null,
          "status": "ongoing",
          "priority": "low",
          "tags": [
            "example"
          ]
        }
      ]
    }
  ]
};
