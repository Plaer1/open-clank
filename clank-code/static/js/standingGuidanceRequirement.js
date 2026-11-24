// Optional standing-guidance requirement: block model-made public file links.
// Unchecked returns the body untouched so requests stay byte-identical to the unchecked flow.
export function withRequirement(body, blockPublicLinks) {
  return blockPublicLinks ? {...body, requirement: {version: 1, effect: 'publish_file', deny: {audience: 'public'}}} : body;
}

export function blocksPublicLinks(item) {
  const r = item && item.requirement;
  return !!r && r.version === 1 && r.effect === 'publish_file' && !!r.deny && r.deny.audience === 'public' && Object.keys(r.deny).length === 1;
}
