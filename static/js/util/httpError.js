// Small, dependency-free helpers for preserving useful HTTP failure details.

const MAX_ERROR_MESSAGE_LENGTH = 600;

function _looksLikeHtml(value) {
  if (typeof value !== 'string') return false;
  return /^\s*<(?:!doctype\s+html\b|html\b|head\b|body\b|title\b|h[1-6]\b|div\b|p\b)/i.test(value);
}

function _cleanMessage(value) {
  if (typeof value !== 'string') return '';
  if (_looksLikeHtml(value)) return '';
  const compact = value.replace(/\s+/g, ' ').trim();
  if (!compact) return '';
  return compact.length > MAX_ERROR_MESSAGE_LENGTH
    ? `${compact.slice(0, MAX_ERROR_MESSAGE_LENGTH - 1)}…`
    : compact;
}

function _messageKey(value) {
  return value.toLocaleLowerCase().replace(/[\s.;:!—-]+$/g, '').trim();
}

function _pushUnique(messages, value) {
  const clean = _cleanMessage(value);
  if (!clean) return;
  const key = _messageKey(clean);
  if (!messages.some((message) => _messageKey(message) === key)) messages.push(clean);
}

function _collectPayloadMessages(value, messages, depth = 0) {
  if (depth > 6 || value === null || value === undefined) return;
  if (typeof value === 'string') {
    _pushUnique(messages, value);
    return;
  }
  if (Array.isArray(value)) {
    for (const item of value) _collectPayloadMessages(item, messages, depth + 1);
    return;
  }
  if (typeof value !== 'object') return;

  // FastAPI validation failures use {loc, msg, type} entries.
  if (typeof value.msg === 'string') {
    const location = Array.isArray(value.loc)
      ? value.loc.filter((part) => part !== 'body').map(String).join('.')
      : '';
    _pushUnique(messages, location ? `${location}: ${value.msg}` : value.msg);
  }

  for (const key of ['message', 'detail', 'details', 'error', 'errors', 'reason', 'cause']) {
    if (Object.prototype.hasOwnProperty.call(value, key)) {
      _collectPayloadMessages(value[key], messages, depth + 1);
    }
  }
}

export function errorPayloadMessage(payload) {
  const messages = [];
  _collectPayloadMessages(payload, messages);
  return messages.join('; ');
}

export async function responseError(response, fallback = 'Request failed') {
  let raw = '';
  try {
    raw = await response.text();
  } catch { /* a status-only fallback is still better than losing the failure */ }

  let payload = null;
  if (raw) {
    const contentType = response?.headers?.get?.('content-type') || '';
    if (/\btext\/html\b/i.test(contentType) || _looksLikeHtml(raw)) {
      const status = Number(response?.status);
      return {
        message: Number.isFinite(status) && status > 0
          ? `${fallback} (HTTP ${status}; server returned an HTML error page)`
          : `${fallback} (server returned an HTML error page)`,
        problem: null,
      };
    }
    try {
      payload = JSON.parse(raw);
      const message = errorPayloadMessage(payload);
      if (message) {
        const detail = payload && typeof payload === 'object' ? payload.detail : null;
        return {
          message,
          problem: detail && typeof detail === 'object' && !Array.isArray(detail)
            ? detail
            : null,
        };
      }
    } catch {
      const message = _cleanMessage(raw);
      if (message) return { message, problem: null };
    }
  }

  const status = Number(response?.status);
  return {
    message: Number.isFinite(status) && status > 0
      ? `${fallback} (HTTP ${status})`
      : fallback,
    problem: null,
  };
}

export async function responseErrorMessage(response, fallback = 'Request failed') {
  return (await responseError(response, fallback)).message;
}

export function contextualErrorMessage(context, detail) {
  const prefix = _cleanMessage(context) || 'Request failed';
  const parts = _cleanMessage(detail)
    .split(/\s*;\s*/)
    .filter(Boolean);
  const unique = [];
  for (const part of parts) _pushUnique(unique, part);
  const message = unique.join('; ') || prefix;
  const prefixKey = _messageKey(prefix);
  const messageKey = _messageKey(message);
  const prefixSuffix = messageKey.startsWith(prefixKey)
    ? messageKey.slice(prefixKey.length)
    : '';
  if (messageKey === prefixKey || /^[\s(:;—-]/.test(prefixSuffix)) return message;
  return `${prefix} — ${message}`;
}
