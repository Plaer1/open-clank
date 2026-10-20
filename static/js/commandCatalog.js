import { NATIVE_SURFACE_ACTIONS } from './nativeSurfaceCatalog.js';
// Shared discovery and resolution. No I/O or native template execution here.
const modes = { chat: 'Read-only answers and safe context', plan: 'Investigate and propose a plan', agent: 'Use the enabled agent tools' };
const slash = value => '/' + String(value || '').replace(/^\//, '');
const key = value => String(value || '').toLowerCase();

export function parseCommandInput(input) {
  const match = String(input).match(/^[/!]([^\s]+)(?:[ \t]|\r?\n)?([\s\S]*)$/);
  return match ? { name: key(match[1]), arguments: match[2] || '' } : null;
}

export function buildCommandCatalog({ commands = {}, legacyAliases = {}, skills = [], nativeState = {}, settingsEntries = [], translate = value => value } = {}) {
  const entries = [];
  const winners = new Map();
  function add(entry, names = [], qualified = '') {
    const row = { aliases: [], scope: 'session', arguments: '[arguments]', availability: 'available', reason: '', ...entry };
    row.token = slash(row.token);
    row.usage ||= `${row.token} ${row.arguments}`.trim();
    row.help ||= '';
    row.category ||= 'Other';
    const appAuthored = entry.appAuthored ?? !['skill', 'native'].includes(row.executor);
    const originalHelp = row.help;
    const originalCategory = row.category;
    const originalReason = row.reason;
    if (appAuthored) {
      try { row.help = String(translate(row.help) ?? row.help); row.category = String(translate(row.category) ?? row.category); row.reason = String(translate(row.reason) ?? row.reason); } catch { /* Startup/unavailable translator preserves authored text. */ }
    }
    row.searchTerms = [...(entry.searchTerms || []), originalHelp, originalCategory, originalReason, row.help, row.category, row.reason];
    row.appAuthored = appAuthored;
    entries.push(row);
    for (const name of [row.token, ...names]) {
      const token = key(slash(name));
      if (!winners.has(token)) winners.set(token, row);
    }
    if (qualified) winners.set(key(slash(qualified)), row);
    return row;
  }
  for (const [name, help] of Object.entries(modes)) add({ identity: `mode:${name}`, token: name, help, category: 'Interaction modes', executor: 'mode', origin: 'host', arguments: '[prompt]', name });
  add({ identity: 'host:literal', token: 'literal', help: 'Send the following text literally, including a leading slash', executor: 'literal', origin: 'host', arguments: '<text>', category: 'Chats' });
  for (const [name, help, argumentsHint] of [
    ['btw', 'Ask a read-only side question in an ephemeral native fork; answer is dismissible', '<question>'],
    ['recover', 'List interrupted candidates; pass a candidate ID to explicitly resume it (busy/retry fenced)', '[assistantMessageID]'],
    ['timeline', 'List user-turn boundaries available for native undo', ''],
    ['undo', 'Revert native history to a mapped user-turn boundary', '[host-message-id]'],
    ['redo', 'Restore the last reversible native history boundary', ''],
    ['history-reconcile', 'Explicitly reconcile an interrupted native history change', ''],
    ['native-fork', 'Fork visible native chat history, optionally before a user turn', '[host-message-id]'],
    ['fork-reconcile', 'Explicitly settle an interrupted native fork candidate', '<child-session-id>'],
    ['agent-profile', 'List or select an eligible named profile for the next Agent turn; the main model stays unchanged', '[id|reset]']
  ]) add({ identity: `operation:${name}`, token: name, name, help, arguments: argumentsHint, origin: 'host/native', executor: 'operation', category: 'Session operations',
    availability: nativeState.available ? 'available' : ['timeline','undo','redo','history-reconcile','native-fork','fork-reconcile','agent-profile'].includes(name) ? 'unavailable' : nativeState.native_route_selected ? 'requires-start' : 'unavailable', reason: ['timeline','undo','redo','history-reconcile','native-fork','fork-reconcile','agent-profile'].includes(name) ? 'Start a native chat before using its session controls' : nativeState.native_route_selected ? 'Explicit invocation starts the selected native session owner' : nativeState.reason || 'Native session is not ready' });
  const hostRows = new Map();
  // Canonical host names are reserved before aliases are considered.
  for (const [name, def] of Object.entries(commands)) {
    const row = add({ identity: `host:${name}`, token: name, help: def.help, usage: def.usage, category: def.category, origin: 'host', executor: 'host', name, scope: def.scope || ({ theme: 'browser-local', memory: 'account', setup: 'account', skills: 'workspace', mcp: 'account', models: 'account', shortcuts: 'browser-local', settings: 'account / browser-local' }[name] || 'session'), hidden: !!def.hidden, aliases: [], definition: def }, [], `host:${name}`);
    hostRows.set(name, row);
    for (const [sub, sdef] of Object.entries(def.subs || {})) {
      const child = add({ identity: `host:${name}:${sub}`, token: `${name} ${sub}`, help: sdef.help, usage: sdef.usage, category: def.category, origin: 'host', executor: 'host', name, sub, scope: sdef.scope || def.scope || ({ memory: 'account', skills: 'workspace' }[name] || 'session'), hidden: !!def.hidden || !!sdef.hidden || sub.startsWith('_'), definition: sdef, aliases: [] }, [], `host:${name} ${sub}`);
      for (const alias of sdef.alias || []) { child.aliases.push(slash(`${name} ${alias}`)); if (!winners.has(key(slash(`${name} ${alias}`)))) winners.set(key(slash(`${name} ${alias}`)), child); }
    }
  }
  for (const [name, def] of Object.entries(commands)) for (const alias of def.alias || []) {
    const token = key(slash(alias));
    if (!winners.has(token)) {
      winners.set(token, hostRows.get(name)); hostRows.get(name).aliases.push(slash(alias));
      for (const [sub, sdef] of Object.entries(def.subs || {})) {
        const child = winners.get(key(slash(`${name} ${sub}`)));
        for (const subName of [sub, ...(sdef.alias || [])]) {
          const childToken = slash(`${alias} ${subName}`);
          if (!winners.has(key(childToken))) { winners.set(key(childToken), child); child.aliases.push(childToken); }
        }
      }
    }
  }
  for (const [alias, target] of Object.entries(legacyAliases)) {
    const row = winners.get(key(slash(`${target.parent} ${target.sub}`))) || hostRows.get(target.parent);
    if (row && !winners.has(key(slash(alias)))) { winners.set(key(slash(alias)), row); row.aliases.push(slash(alias)); }
  }
  for (const alias of ['web', 'bash', 'doc', 'sidebar', 'forget']) {
    const row = winners.get(key(slash(alias)));
    if (row?.hidden && legacyAliases[alias]) entries.push({ ...row, token: slash(alias), aliases: [], usage: slash(alias), hidden: false });
  }
  for (const setting of settingsEntries) add({ ...setting, token: setting.token, executor: 'settings', origin: setting.origin || 'host' });
  for (const skill of skills) {
    if (skill.available === false || skill.eligible === false || skill.trusted === false || skill.revoked || skill.disabled || ['draft','staged'].includes(skill.state)) continue;
    const name = String(skill.name || skill.token?.replace(/^\//, '') || '');
    if (!name) continue;
    const row = add({ ...skill, identity: `skill:${name}`, name, token: `skill:${name}`, aliases: [], origin: skill.origin || 'skill', executor: 'skill', scope: skill.scope || 'workspace', category: skill.category || 'Skills', arguments: skill.arguments || '<request>', help: skill.help || skill.description || 'Run eligible skill' }, [], `skill:${name}`);
    for (const alias of [name, ...(skill.aliases || [])]) if (!winners.has(key(slash(alias)))) { winners.set(key(slash(alias)), row); row.aliases.push(slash(alias)); }
  }
  const defaultDescriptions = {
    init: 'Set up project AGENTS.md; may change files', review: 'Review changes, commit, branch or PR in a delegated task',
    dream: 'Consolidate project memory [focus]', distill: 'Package repeated work into skills and commands [focus]',
    goal: 'Set/queue an objective; empty cancels. pause/resume/status/list/clear/reset/cancel/clear-history/edit <revision> <objective>/verify [id revision]',
    rebuild: 'Rebuild checkpoint context; reports fallback or unchanged outcome', 'deep-research': 'Gated deep research workflow [request]', loops: 'Gated scheduled job management [cancel|delete|stop|kill IDs]', compact: 'Compact persistent native session context'
  };
  const negotiated = nativeState.commands || [];
  const nativeCommands = [...negotiated, ...Object.entries(defaultDescriptions).filter(([name]) => !negotiated.some(command => command.name === name)).map(([name, description]) => ({ name, description, available: !nativeState.available && nativeState.native_route_selected && !['deep-research','loops'].includes(name), coldStart: nativeState.native_route_selected && !nativeState.available && !['deep-research','loops'].includes(name), reason: nativeState.native_route_selected && !nativeState.available && !['deep-research','loops'].includes(name) ? 'Explicit invocation starts the selected native session owner; registry is revalidated by native execution' : nativeState.available ? 'Not negotiated; feature gate or registry entry is unavailable' : nativeState.reason || 'Native engine is not ready in this session' }))];
  for (const command of nativeCommands) {
    const name = command.name;
    if (!name) continue;
    const available = (nativeState.available || command.coldStart) && command.available !== false;
    const row = add({ ...command, appAuthored: !negotiated.includes(command), identity: `native:${command.origin || command._meta?.source || 'native'}:${name}`, name, token: `mimo:${name}`, aliases: [], help: name === 'goal' ? defaultDescriptions.goal : command.description || command.help || 'Run native command', origin: command.origin || command._meta?.source || 'native', executor: 'native', scope: command.scope || 'session', category: 'Agent commands', arguments: command.arguments || command.argumentHint || (command.hints || command._meta?.hints || []).join(' ') || '[arguments]', availability: command.coldStart ? 'requires-start' : available ? 'available' : 'unavailable', reason: command.reason || nativeState.reason || 'Native engine is not ready in this session' }, [], `mimo:${name}`);
    for (const alias of [name, ...(command.aliases || [])]) if (!winners.has(key(slash(alias)))) { winners.set(key(slash(alias)), row); row.aliases.push(slash(alias)); }
  }
  for (const action of NATIVE_SURFACE_ACTIONS) {
    // Non-slash keyboard/composer actions remain in the coverage ledger. Only
    // intentional slash actions belong in invocation discovery.
    if (!action.name && !action.settingsDestination) continue;
    const equivalent = action.catalogToken && winners.get(key(slash(action.catalogToken)));
    if (equivalent) {
      const qualified = slash(`tui:${action.id}`);
      winners.set(key(qualified), equivalent);
      equivalent.aliases.push(qualified);
      equivalent.nativeActions = [...(equivalent.nativeActions || []), action.id];
      continue;
    }
    const collision = !action.name || winners.has(key(slash(action.name)));
    const settingsDestination = action.settingsDestination || {
      'theme.switch': { panel: 'appearance' }, 'language.switch': { panel: 'appearance' },
      'plugins.list': { panel: 'integrations' }, 'plugins.install': { panel: 'integrations' }
    }[action.id];
    const row = add({ identity: `tui:${action.id}`, name: action.name, token: collision ? `tui:${action.id}` : action.name,
      origin: 'native TUI', executor: settingsDestination ? 'settings' : 'unavailable', category: 'Native actions', scope: settingsDestination ? (settingsDestination.panel === 'appearance' ? 'browser-local' : 'account') : action.status === 'terminal-only' ? 'terminal' : 'session', arguments: '',
      help: settingsDestination ? action.settingsDestination ? action.help : 'Open ' + settingsDestination.panel + ' Settings' : action.help, availability: settingsDestination ? 'available' : 'unavailable', reason: action.reason, destination: settingsDestination || action.destination,
      disposition: action.status }, [], `tui:${action.id}`);
    for (const alias of collision ? [] : action.aliases) if (!winners.has(key(slash(alias)))) { winners.set(key(slash(alias)), row); row.aliases.push(slash(alias)); }
  }
  // Advertise bare winners while retaining a qualified escape for every source.
  for (const row of entries) {
    if (['skill','native'].includes(row.executor)) {
      const bare = slash(row.name);
      if (winners.get(key(bare)) === row) { const qualified = row.token; row.token = bare; row.aliases = row.aliases.filter(a => a !== bare); row.aliases.push(qualified); row.usage = `${bare} ${row.arguments}`.trim(); }
    }
    if (row.executor === 'host' && winners.get(key(row.token))?.identity !== row.identity) { row.token = slash(`host:${row.name}${row.sub ? ' ' + row.sub : ''}`); row.usage = `${row.token} ${row.arguments}`.trim(); }
  }
  return { entries, winners, nativeState };
}

export function resolveCatalogCommand(catalog, input) {
  const parsed = parseCommandInput(input);
  if (!parsed) return null;
  const root = slash(parsed.name);
  const exact = catalog.winners.get(key(root));
  // Longest token wins for Settings field destinations and grouped commands;
  // raw arguments retain spacing after the one consumed separator.
  let entry = exact;
  let argumentText = parsed.arguments;
  const candidates = [...catalog.winners.keys()].filter(token => token.startsWith(key(root) + ' ')).sort((a,b) => b.length - a.length);
  for (const token of candidates) {
    const suffix = token.slice(root.length + 1);
    if (key(parsed.arguments) === suffix || key(parsed.arguments).startsWith(suffix + ' ') || key(parsed.arguments).startsWith(suffix + '\n')) {
      entry = catalog.winners.get(token); argumentText = parsed.arguments.slice(suffix.length + (parsed.arguments.length > suffix.length ? 1 : 0)); break;
    }
  }
  return { ...parsed, entry: entry || null, arguments: argumentText };
}

export function filterCommandCatalog(catalog, query = '', { page = 1, pageSize = 40 } = {}) {
  const q = key(query).replace(/^\//, '').trim();
  const rows = catalog.entries.filter(e => !e.hidden && (!q || [e.token, ...e.aliases, ...(e.searchTerms || []), e.help, e.category, e.origin, e.scope, e.identity].some(value => key(value).includes(q))));
  const pages = Math.max(1, Math.ceil(rows.length / pageSize));
  const current = Math.min(pages, Math.max(1, Number(page) || 1));
  return { entries: rows.slice((current - 1) * pageSize, current * pageSize), total: rows.length, page: current, pages };
}
