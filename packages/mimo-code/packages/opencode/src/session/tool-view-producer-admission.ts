/** Private per-invocation optimization witness; never serialized or a grant. */
export type Profile = 'terminal-ansi-v1' | 'html-markdown-v1' | 'structured-search-v1' | 'repeated-lines-v1' | 'unified-diff-v1' | 'tap13-v1' | 'unittest-success-v1' | 'cargo-artifacts-v1' | 'untracked-status-v1' | 'base64-lines-v1' | 'grep-rows-v1' | 'line-cap-v1' | 'json-shape-v1' | 'read-unnumbered-v1' | 'ndjson-logs-v1' | 'json-whitespace-v1' | 'toon-v1' | 'progress-redraw-v1' | 'npm-lockfile-versions-v1' | 'markdown-slim-v1' | 'read-json-whitespace-v1' | 'read-repeated-lines-v1'
let admitted:WeakMap<object,{profiles:Profile[];current:()=>boolean}> | undefined
export function bindToolViewProducer(context:object,profile:Profile | Profile[],current:()=>boolean):void {
  admitted ??= new WeakMap()
  admitted.set(context,{profiles:Array.isArray(profile) ? profile : [profile],current})
}
export function toolViewProducerCurrent(context:object,profile:Profile):boolean {
  const value=admitted?.get(context)
  if (!value || !value.profiles.includes(profile)) return false
  try {return value.current() === true} catch {return false}
}
