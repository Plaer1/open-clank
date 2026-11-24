// Data-only projection of the theme owner's synchronous state. No settings store.
const STANDARD = Object.freeze({ id: 'open-clank.deliverable-design', version: '1.0.0', sha256: '21708f3285fd26c50100db6b4467e08b58461ca60ae835ac6f3ea006bdf36675' });
const ADVANCED = ['userBubbleBg','aiBubbleBg','bubbleBorder','sidebarBg','brandColor','brandMixTo','hamburgerColor','inputBg','inputBorder','sendBtnBg','sendBtnHover','codeBg','codeFg','toggleActive','accentPrimary','accentHover','accentFocus','accentDisabled','accentError','successColor'];
const ROLES = {
  semantic: ['accent','accent-primary','accent-hover','accent-focus','accent-selection','accent-disabled','accent-error','color-accent','color-error','color-error-light','color-danger','color-success','color-warning','color-muted','color-muted-alt','color-agent-active','color-link-hover','green','warn'],
  material: ['clanker-field','clanker-surface','clanker-surface-2','clanker-well','clanker-ink','clanker-soft-ink','clanker-rail','clanker-control','clanker-bevel','clanker-outline','clanker-raised','clanker-cobalt','clanker-sky','clanker-lime','clanker-gold','clanker-coral','clanker-pink','clanker-lilac'],
};
const cleanText = (value, fallback = '') => typeof value === 'string' && /^[\w .,'-]{1,100}$/.test(value) ? value : fallback;
const freeze = value => { if (value && typeof value === 'object') { Object.values(value).forEach(freeze); Object.freeze(value); } return value; };

/** Project a normalized snapshot and already-resolved CSS values. The caller owns
 * account fencing and must collect both synchronously from the same live theme. */
export function projectDeliverableAppearance(snapshot, resolved, { outputReducedMotion = false, fontFamily = '' } = {}) {
  if (!snapshot?.identity || !snapshot?.palette || !resolved) return null;
  const colors = {};
  for (const key of ['bg','fg','panel','border','red']) if (/^#[0-9a-f]{3,8}$/i.test(snapshot.palette[key] || '')) colors[key] = snapshot.palette[key];
  if (Object.keys(colors).length !== 5) return null;
  const advanced = {};
  for (const key of ADVANCED) {
    const value = resolved.advanced?.[key];
    if (typeof value === 'string' && /^(?:rgba?\([^;{}]+\)|#[0-9a-f]{3,8}|color\([^;{}]+\))$/i.test(value)) advanced[key] = value;
  }
  const roles = {};
  for (const group of Object.keys(ROLES)) {
    roles[group] = {};
    for (const key of ROLES[group]) {
      const value = resolved[group]?.[key];
      if (typeof value === 'string' && /^(?:rgba?\([^;{}]+\)|#[0-9a-f]{3,8}|color\([^;{}]+\))$/i.test(value)) roles[group][key] = value;
    }
  }
  return freeze({ standard: { ...STANDARD }, identity: { name: cleanText(snapshot.identity.name, 'custom'), kind: snapshot.identity.kind === 'builtin' ? 'builtin' : 'custom', overridePresent: !!snapshot.identity.overridePresent }, colors, advanced, ...roles,
    typography: { font: cleanText(snapshot.typography?.font, 'liga-comic-mono'), family: cleanText(fontFamily, 'monospace'), density: cleanText(snapshot.typography?.density, 'comfortable') },
    layout: { edgeHardness: Number.isFinite(snapshot.layout?.edgeHardness) ? snapshot.layout.edgeHardness : 50 },
    motion: { reduced: outputReducedMotion === true },
    provenance: { overrideColors: Object.keys(snapshot.overrides?.colors || {}).filter(key => Object.hasOwn(colors,key)), source: 'current-applied-theme' },
  });
}

// Resolve variables in their body inheritance context, including color-mix().
// Unset roles are omitted; no var()/color-mix() expression enters the result.
export function readDeliverableThemeRoles(doc = document) {
  if (!doc?.body || !doc.defaultView?.getComputedStyle) return null;
  const probe = doc.createElement('span');
  probe.setAttribute('aria-hidden', 'true');
  probe.style.cssText = 'position:absolute;visibility:hidden;pointer-events:none';
  doc.body.appendChild(probe);
  try {
    const bodyStyle = doc.defaultView.getComputedStyle(doc.body);
    const read = name => {
      if (!bodyStyle.getPropertyValue('--' + name).trim()) return '';
      probe.style.color = '';
      probe.style.color = `var(--${name})`;
      return doc.defaultView.getComputedStyle(probe).color;
    };
    const output = { semantic: {}, material: {}, advanced: {} };
    for (const group of Object.keys(ROLES)) for (const name of ROLES[group]) output[group][name] = read(name);
    const advancedCss = { userBubbleBg:'user-bubble-bg', aiBubbleBg:'ai-bubble-bg', bubbleBorder:'bubble-border', sidebarBg:'sidebar-bg', brandColor:'brand-color', brandMixTo:'brand-mix-to', hamburgerColor:'hamburger-color', inputBg:'input-bg', inputBorder:'input-border', sendBtnBg:'send-btn-bg', sendBtnHover:'send-btn-hover', codeBg:'code-bg', codeFg:'code-fg', toggleActive:'toggle-active', accentPrimary:'accent-primary', accentHover:'accent-hover', accentFocus:'accent-focus', accentDisabled:'accent-disabled', accentError:'accent-error', successColor:'color-success' };
    for (const [key, name] of Object.entries(advancedCss)) output.advanced[key] = read(name);
    return output;
  } finally { probe.remove(); }
}

// Portable, passive output styling from a captured appearance. Never evaluate
// arbitrary CSS, load fonts, read global theme state or embed URLs.
const PORTABLE_COLOR = /^(?:#(?:[0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})|rgba?\([\d.,%\s/+-]+\)|color\(srgb\s+[\d.\s/%+-]+\))$/i;
const PORTABLE_FONTS = Object.freeze({
  'comic-clank': "'Comic Clank', sans-serif",
  'comic-clank-mono': "'Comic Clank Mono', monospace",
  'liga-comic-mono': "'Liga Comic Mono', 'Fira Code', monospace",
  mono: "'Fira Code', monospace", sans: 'system-ui, sans-serif',
  serif: 'Georgia, serif', opendyslexic: "'OpenDyslexic', sans-serif",
});

/** Saved data in; immutable, allowlisted CSS variables and appearance data out. */
export function portableDeliverableStyle(appearance) {
  if (!appearance || typeof appearance !== 'object' || !appearance.colors) return null;
  const variables = {};
  for (const name of ['bg','fg','panel','border','red']) {
    const value = appearance.colors[name];
    if (typeof value !== 'string' || !PORTABLE_COLOR.test(value)) return null;
    variables[`--oc-${name}`] = value;
  }
  for (const group of ['semantic','material']) for (const name of ROLES[group]) {
    const value = appearance[group]?.[name];
    if (typeof value === 'string' && PORTABLE_COLOR.test(value)) variables[`--oc-${name}`] = value;
  }
  const fontId = typeof appearance.typography?.font === 'string' ? appearance.typography.font : '';
  const knownFont = Object.hasOwn(PORTABLE_FONTS, fontId);
  const family = knownFont ? PORTABLE_FONTS[fontId] : 'monospace';
  const density = ['compact','comfortable','spacious'].includes(appearance.typography?.density) ? appearance.typography.density : 'comfortable';
  const hardness = Number.isFinite(appearance.layout?.edgeHardness) ? Math.max(0,Math.min(100,appearance.layout.edgeHardness)) : 50;
  variables['--oc-font-family'] = family;
  variables['--oc-radius-scale'] = String((100-hardness)/50);
  const standard = appearance.standard;
  const validStandard = standard?.id === STANDARD.id && standard?.version === STANDARD.version && standard?.sha256 === STANDARD.sha256;
  return freeze({cssVariables:variables, typography:{font:knownFont ? fontId : 'fallback', family, density},
    layout:{edgeHardness:hardness}, motion:{reduced:appearance.motion?.reduced === true},
    standard:validStandard ? {...STANDARD} : null, fontAvailability:'unverified-not-embedded'});
}
