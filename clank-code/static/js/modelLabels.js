// Shared provider labels for every recipient-facing model picker.

export const UNKNOWN_PROVIDER_LABEL = 'Unknown provider';

function clean(value) {
  return String(value || '').trim();
}

/**
 * Use only an explicit provider display value. Model IDs are deliberately not
 * parsed because bare IDs and repeated IDs across providers are ambiguous.
 */
export function providerDisplayName(value, unknownLabel = UNKNOWN_PROVIDER_LABEL) {
  const name = clean(value);
  if (!name || /^shared\s+provider$/i.test(name)) return unknownLabel;
  return name;
}

export function sharedProviderLabel(value, unknownLabel = UNKNOWN_PROVIDER_LABEL) {
  return `Shared ${providerDisplayName(value, unknownLabel)}`;
}

export function sharedSecondaryLabel({ label = '', owner = '' } = {}) {
  const custom = clean(label);
  const attribution = clean(owner) ? `Shared by ${clean(owner)}` : '';
  return [custom, attribution].filter(Boolean).join(' · ');
}

export default {
  providerDisplayName,
  sharedProviderLabel,
  sharedSecondaryLabel,
};
