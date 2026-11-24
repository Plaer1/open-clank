/** Closed Cargo-shaped success records; output grammar, not executable provenance. */
import {parseJsonInput, type Node} from '../util/toon-subset'

const clean = (s:string) => !/[\u0000-\u001f\u007f-\u009f]/u.test(s)
const object = (node:Node | undefined, required:readonly string[], optional:readonly string[] = []):Map<string,Node> | undefined => {
  if (!node || node.k !== 'o') return
  const map = new Map(node.v)
  if (required.some(key => !map.has(key)) || [...map.keys()].some(key => !required.includes(key) && !optional.includes(key))) return
  return map
}
const string = (node:Node | undefined):string | undefined =>
  node?.k === 'a' && typeof node.v === 'string' && clean(node.v) ? node.v : undefined
const bool = (node:Node | undefined):boolean | undefined =>
  node?.k === 'a' && typeof node.v === 'boolean' ? node.v : undefined
const strings = (node:Node | undefined):string[] | undefined => {
  if (!node || node.k !== 'l' || node.v.length > 256) return
  const result:string[] = []
  for (const entry of node.v) {const value=string(entry);if (value === undefined) return;result.push(value)}
  return result
}

export function summarizeCargoArtifacts(input:string): {body:string; omitted:number} | undefined {
  if (typeof input !== 'string' || Buffer.byteLength(input,'utf8') > 51200 || !input.endsWith('\n') || input.includes('\r')) return
  const lines=input.slice(0,-1).split('\n')
  if (lines.length < 2 || lines.length > 200 || lines.some(line => !line)) return
  try {
    const summary:{package_id:string;target:{name:string;kind:string[]};filenames:string[];executable:string|null;fresh:boolean}[]=[]
    for (const line of lines.slice(0,-1)) {
      const record=object(parseJsonInput(line),['reason','package_id','manifest_path','target','profile','features','filenames','executable','fresh'])
      if (!record || string(record.get('reason')) !== 'compiler-artifact') return
      const pkg=string(record.get('package_id')), manifest=string(record.get('manifest_path'))
      const target=object(record.get('target'),['kind','crate_types','name','src_path','edition','doc','doctest','test'],['required-features'])
      const profile=object(record.get('profile'),['opt_level','debuginfo','debug_assertions','overflow_checks','test'])
      const features=strings(record.get('features')), filenames=strings(record.get('filenames')), fresh=bool(record.get('fresh'))
      const executableNode=record.get('executable')
      const executable=executableNode?.k === 'a' && executableNode.v === null ? null : string(executableNode)
      if (pkg === undefined || manifest === undefined || !target || !profile || features === undefined || filenames === undefined || fresh === undefined || executable === undefined) return
      const kind=strings(target.get('kind')), crateTypes=strings(target.get('crate_types')), name=string(target.get('name'))
      if (kind === undefined || crateTypes === undefined || name === undefined || string(target.get('src_path')) === undefined || string(target.get('edition')) === undefined ||
          ['doc','doctest','test'].some(key=>bool(target.get(key)) === undefined) ||
          (target.has('required-features') && strings(target.get('required-features')) === undefined)) return
      const debug=profile.get('debuginfo')
      if (string(profile.get('opt_level')) === undefined || !debug || debug.k !== 'a' ||
          !(debug.v === null || debug.v === 0 || debug.v === 1 || debug.v === 2 || debug.v === 'line-directives-only' || debug.v === 'line-tables-only') ||
          ['debug_assertions','overflow_checks','test'].some(key=>bool(profile.get(key)) === undefined)) return
      summary.push({package_id:pkg,target:{name,kind},filenames,executable,fresh})
    }
    const terminal=object(parseJsonInput(lines[lines.length-1]!),['reason','success'])
    if (!terminal || string(terminal.get('reason')) !== 'build-finished' || bool(terminal.get('success')) !== true) return
    return {body:JSON.stringify(summary)+'\n'+lines[lines.length-1]+'\n',omitted:summary.length}
  } catch { return }
}
