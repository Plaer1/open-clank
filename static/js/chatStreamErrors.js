/** Build a terminal stream error without discarding a provider's readable text. */
export function createTerminalStreamError(payload = {}, displayMessage = '') {
  const rawError = payload.error;
  const message = displayMessage
    || payload.text
    || (typeof rawError === 'string' ? rawError : rawError?.message)
    || `Error ${payload.status || 'unknown'}`;
  const error = new Error(message);
  error.name = 'TerminalStreamError';
  error.terminalStreamError = true;
  error.status = payload.status;
  return error;
}

/** Only connection-class failures are safe candidates for automatic recovery. */
export function isRecoverableStreamError(error) {
  if (!error || error.terminalStreamError || error.name === 'TerminalStreamError') return false;
  if (error.name === 'TypeError') return true;
  const message = (error.message || '').toLowerCase();
  if (/\btool\b|unsupported|json|parse|\b4\d\d\b|\b5\d\d\b/.test(message)) return false;
  return /network|fetch|connection|reset|closed|aborted|stream|tim(?:e|ed)\s?out|econn|eof/.test(message);
}
