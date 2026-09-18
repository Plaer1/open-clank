export interface OutlineEntry {
  line: number;
  level: number;
  text: string;
  kind: 'heading' | 'list';
  endLine?: number;
  [key: string]: unknown;
}

export interface OutlineNode extends OutlineEntry {
  children: OutlineNode[];
}

export function outlineEntries(source: string): OutlineEntry[];
export function outlineTree(entries: OutlineEntry[]): OutlineNode[];
export function flattenTree(tree: OutlineNode[]): OutlineNode[];
export function reparentHeading(source: string, line: number, newLevel: number): string;
export function moveHeadingSection(source: string, line: number, delta: number): string;
export function moveHeadingSectionTo(source: string, line: number, targetLine: number): string;
export function databaseRelations(...args: unknown[]): unknown[];
