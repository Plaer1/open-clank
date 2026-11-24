// Deterministic backend hints from the canonical browser language registry.
// These associations never replace Rust content/binary validation or Files grants.
import { readFileSync, writeFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { LANGUAGE_REGISTRY, EDITOR_CONDITIONAL_TEXT_SUFFIXES, EDITOR_BINARY_SUFFIX_PATTERN } from '../static/js/editor/languageRegistry.js';
const unique = values => [...new Set(values.map(value => value.toLowerCase()))].sort();
const selectable = LANGUAGE_REGISTRY.filter(entry => entry.selectable);
const registry = new URL('../static/js/editor/languageRegistry.js',import.meta.url);
const output = new URL('../src/openclank/editor_language_associations.json',import.meta.url);
const manifest = {
  version:1,
  generatedFrom:'static/js/editor/languageRegistry.js',
  sourceSha256:createHash('sha256').update(readFileSync(registry)).digest('hex'),
  binarySuffixPattern:EDITOR_BINARY_SUFFIX_PATTERN,
  suffixes:unique(selectable.flatMap(entry => entry.extensions).filter(suffix => !/[?*]/.test(suffix))),
  filenames:unique(selectable.flatMap(entry => entry.filenames)),
  patterns:unique(selectable.flatMap(entry => [...entry.patterns,...entry.extensions.filter(suffix => /[?*]/.test(suffix))])),
  conditionalSuffixes:[...EDITOR_CONDITIONAL_TEXT_SUFFIXES].sort(),
};
const body = JSON.stringify(manifest,null,2)+'\n';
let previous='';try { previous=readFileSync(output,'utf8'); } catch (error) { if (error.code !== 'ENOENT') throw error; }
if (previous !== body) writeFileSync(output,body);
console.log(`Editor language hints: ${manifest.suffixes.length} suffixes, ${manifest.filenames.length} basenames, ${manifest.patterns.length} patterns`);
