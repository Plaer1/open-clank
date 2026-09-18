export const WIKI_PRESENTATION_VERSION = 1;

const finite = (value, fallback = 0) => Number.isFinite(Number(value)) ? Math.max(0, Number(value)) : fallback;
const uniqueKnown = (values, known) => [...new Set(Array.isArray(values) ? values.map(String) : [])].filter(id => known.has(id));

/** Presentation only. Document content and authority never live in this value. */
export function normalizeWikiPresentation(value = {}, documentIds = [], defaultStory = []) {
  const known = new Set(documentIds.map(String));
  const initialized = value?.version === WIKI_PRESENTATION_VERSION && value.initialized === true;
  const story = initialized ? uniqueKnown(value.story, known) : uniqueKnown(defaultStory, known);
  const pinned = uniqueKnown(value.pinned, known).filter(id => story.includes(id));
  const editing = uniqueKnown(value.editing, known).filter(id => story.includes(id));
  const cards = {};
  for (const id of story) {
    const source = value?.cards?.[id];
    if (!source || typeof source !== 'object') continue;
    cards[id] = {
      scrollTop:finite(source.scrollTop), selectionStart:finite(source.selectionStart),
      selectionEnd:finite(source.selectionEnd, finite(source.selectionStart)),
    };
  }
  return {
    version:WIKI_PRESENTATION_VERSION, initialized:true, story, pinned, editing, cards,
    libraryScrollTop:finite(value?.libraryScrollTop), storyScrollLeft:finite(value?.storyScrollLeft),
  };
}

export function serializeWikiPresentation(value) {
  return JSON.stringify(normalizeWikiPresentation(value, value?.story || []));
}

export function moveWikiCard(story, id, delta) {
  const next = [...story]; const index = next.indexOf(String(id)); const target = index + Number(delta);
  if (index < 0 || target < 0 || target >= next.length) return next;
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}

export function closeWikiCard(presentation, id) {
  const key = String(id);
  if (presentation.pinned.includes(key)) return { ...presentation };
  const cards = { ...presentation.cards }; delete cards[key];
  return {
    ...presentation, story:presentation.story.filter(value => value !== key),
    editing:presentation.editing.filter(value => value !== key), cards,
  };
}
