function lineEndings(value) {
  return String(value ?? '').replace(/\r\n?/g, '\n');
}

function meaningfulRounds(value) {
  if (!Array.isArray(value)) return [];
  return value.map((item) => (typeof item === 'string' ? item : ''));
}

function joinedVisibleRounds(rounds) {
  return rounds.filter((item) => item.trim()).join('\n\n');
}

function toolEventId(event) {
  if (!event || typeof event !== 'object') return '';
  return String(event.id || event.tool_call_id || event.toolCallId || '').trim();
}

function validTerminalRounds(content, metadata, toolEvents) {
  const layout = metadata?.transcript_v2;
  if (!layout || layout.version !== 1 || layout.content_length !== content.length) return null;
  if (!Array.isArray(layout.blocks)) return null;

  const knownTools = new Set(toolEvents.map(toolEventId).filter(Boolean));
  const answers = [];
  let maxRound = 0;
  for (const block of layout.blocks) {
    if (!block || typeof block !== 'object') return null;
    if (block.kind === 'tool') {
      const callId = String(block.call_id || '').trim();
      if (!callId || !knownTools.has(callId)) return null;
      maxRound = Math.max(maxRound, Number(block.round) || 1);
      continue;
    }
    if (block.kind === 'answer') {
      const start = Number(block.start);
      const end = Number(block.end);
      if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end < start || end > content.length) {
        return null;
      }
      answers.push({ start, end, round: Math.max(0, Number(block.round) || 0) });
      maxRound = Math.max(maxRound, Number(block.round) || 0);
      continue;
    }
    if (block.kind === 'thinking') {
      const thinking = String(metadata?.thinking || '');
      const start = Number(block.start);
      const end = Number(block.end);
      if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end < start || end > thinking.length) {
        return null;
      }
      continue;
    }
    return null;
  }

  answers.sort((a, b) => a.start - b.start);
  let covered = 0;
  for (const answer of answers) {
    if (answer.start !== covered) return null;
    covered = answer.end;
  }
  if (content && covered !== content.length) return null;
  if (!content && answers.length) return null;

  const rounds = Array(Math.max(maxRound + 1, 1)).fill('');
  for (const answer of answers) {
    rounds[answer.round] += content.slice(answer.start, answer.end);
  }
  return rounds;
}

/**
 * Return the only prose layout the rich history renderer may use.
 * Canonical content always wins over missing, partial, duplicated, or stale
 * legacy round_texts. A blank historical content field may recover from
 * meaningful legacy rounds because there is no canonical text to contradict.
 */
export function normalizeAssistantTranscript(content, metadata = {}) {
  const canonical = typeof content === 'string' ? content : String(content ?? '');
  const toolEvents = Array.isArray(metadata?.tool_events) ? metadata.tool_events : [];

  const terminalRounds = validTerminalRounds(canonical, metadata, toolEvents);
  if (terminalRounds) {
    return { roundTexts: terminalRounds, source: 'transcript_v2', layoutValid: true };
  }

  const legacy = meaningfulRounds(metadata?.round_texts);
  const legacyJoined = joinedVisibleRounds(legacy);
  if (canonical) {
    if (legacyJoined && lineEndings(legacyJoined) === lineEndings(canonical)) {
      return { roundTexts: legacy, source: 'legacy-matched', layoutValid: false };
    }
    const lastToolRound = toolEvents.reduce(
      (highest, event) => Math.max(highest, Math.max(1, Number(event?.round) || 1)),
      0,
    );
    const fallback = Array(lastToolRound + 1).fill('');
    fallback[lastToolRound] = canonical;
    return { roundTexts: fallback, source: 'canonical-fallback', layoutValid: false };
  }

  if (legacyJoined) {
    return { roundTexts: legacy, source: 'legacy-recovery', layoutValid: false };
  }
  return { roundTexts: [], source: 'empty', layoutValid: false };
}

export default { normalizeAssistantTranscript };
