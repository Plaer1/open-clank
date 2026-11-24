/** Private completed-Bash producer evidence for selected text profiles. */
import {createHash} from 'node:crypto'
import {toolViewProducerCurrent} from './tool-view-producer-admission'
import {markTerminalAnsi,stripTerminalSgr} from './tool-view-terminal-ansi'
import {encodeRepeatedLines} from './tool-view-repeated-lines'
import {elideBase64Lines} from './tool-view-base64'
import {encodeToonSubset} from '../util/toon-subset'
import {compactJsonTokens} from './tool-view-json'
import {bashJsonView} from './tool-view-json-lines'
import {compactNdjsonLogs} from './tool-view-ndjson'
import {summarizeTap13} from './tool-view-tap13'
import {summarizeUnittest} from './tool-view-unittest'
import {summarizeCargoArtifacts} from './tool-view-cargo-artifacts'
import {summarizeUntrackedStatus} from './tool-view-untracked-status'
import {jsonShape} from './tool-view-json-shape'
import {progressRedraw} from './tool-view-progress-redraw'
import {summarizeUnifiedDiff} from './tool-view-unified-diff'
type Result={output:string;metadata:Record<string,unknown>;attachments?:unknown[]}
type Profile='untracked-status-v1'|'cargo-artifacts-v1'|'unittest-success-v1'|'unified-diff-v1'|'repeated-lines-v1'|'tap13-v1'|'base64-lines-v1'|'ndjson-logs-v1'|'json-whitespace-v1'|'toon-v1'|'json-shape-v1'|'progress-redraw-v1'
type Enabled={diff:boolean;ansi:boolean;repetition:boolean;tap13?:boolean;unittest?:boolean;cargo?:boolean;untracked?:boolean;base64?:boolean;ndjson?:boolean;json?:boolean;toon?:boolean;shape?:boolean;progress?:boolean}
let witnesses:WeakMap<object,{profile:Profile;sha:string;bytes:number;metadata:string}>|undefined
const sha=(s:string)=>createHash('sha256').update(s,'utf8').digest('hex')
const eligible=(v:Result)=>typeof v.output==='string' && !!v.metadata && v.metadata.exit===0 && v.metadata.truncated===false && v.metadata.outputPath===undefined && !v.attachments?.length && Buffer.byteLength(v.output,'utf8')<=51200
/** Select only uniform scalar-record arrays accepted by the canonical codec. */
export function eligibleBashToonTable(input:string):boolean {
  const encoded=encodeToonSubset(input), compact=encoded ? compactJsonTokens(input) : undefined
  if(!encoded || compact===undefined || Buffer.byteLength(encoded.text,'utf8')>=Buffer.byteLength(compact,'utf8'))return false
  try {
    const rows=JSON.parse(input)
    if(!Array.isArray(rows) || rows.length<2 || rows.some(row=>!row || typeof row!=='object' || Array.isArray(row)))return false
    const keys=Object.keys(rows[0]);if(!keys.length)return false
    return rows.every(row=>{const other=Object.keys(row);return other.length===keys.length && other.every((key,i)=>key===keys[i]) &&
      other.every(key=>row[key]===null || ['string','boolean','number'].includes(typeof row[key]))})
  } catch {return false}
}
export function selectBashView(input:string,enabled:Enabled):Profile|'terminal-ansi-v1'|undefined {
  if(enabled.diff && summarizeUnifiedDiff(input)!==undefined)return 'unified-diff-v1'
  if(enabled.ansi && stripTerminalSgr(input)!==undefined)return 'terminal-ansi-v1'
  if(enabled.tap13 && summarizeTap13(input)!==undefined)return 'tap13-v1'
  if(enabled.unittest && summarizeUnittest(input)!==undefined)return 'unittest-success-v1'
  if(enabled.cargo && summarizeCargoArtifacts(input)!==undefined)return 'cargo-artifacts-v1'
  if(enabled.untracked && summarizeUntrackedStatus(input)!==undefined)return 'untracked-status-v1'
  if(enabled.ndjson && compactNdjsonLogs(input)!==undefined)return 'ndjson-logs-v1'
  if(enabled.progress && progressRedraw(input)!==undefined)return 'progress-redraw-v1'
  if(enabled.toon && eligibleBashToonTable(input))return 'toon-v1'
  if(enabled.shape && jsonShape(input)!==undefined)return 'json-shape-v1'
  if(enabled.json && bashJsonView(input)!==undefined)return 'json-whitespace-v1'
  if(enabled.base64 && elideBase64Lines(input)!==undefined)return 'base64-lines-v1'
  if(enabled.repetition && encodeRepeatedLines(input)!==undefined)return 'repeated-lines-v1'
}
export function markBashView<T extends Result>(result:T,context:object):T {
  const enabled={diff:toolViewProducerCurrent(context,'unified-diff-v1'),ansi:toolViewProducerCurrent(context,'terminal-ansi-v1'),repetition:toolViewProducerCurrent(context,'repeated-lines-v1'),tap13:toolViewProducerCurrent(context,'tap13-v1'),unittest:toolViewProducerCurrent(context,'unittest-success-v1'),cargo:toolViewProducerCurrent(context,'cargo-artifacts-v1'),untracked:toolViewProducerCurrent(context,'untracked-status-v1'),base64:toolViewProducerCurrent(context,'base64-lines-v1'),ndjson:toolViewProducerCurrent(context,'ndjson-logs-v1'),json:toolViewProducerCurrent(context,'json-whitespace-v1'),toon:toolViewProducerCurrent(context,'toon-v1'),shape:toolViewProducerCurrent(context,'json-shape-v1'),progress:toolViewProducerCurrent(context,'progress-redraw-v1')}
  if (!enabled.diff&&!enabled.ansi&&!enabled.repetition&&!enabled.tap13&&!enabled.unittest&&!enabled.cargo&&!enabled.untracked&&!enabled.base64&&!enabled.ndjson&&!enabled.json&&!enabled.toon&&!enabled.shape&&!enabled.progress) return result
  try {
    if (!eligible(result)) return result
    const profile=selectBashView(result.output,enabled)
    if(profile==='terminal-ansi-v1')return markTerminalAnsi(result,context)
    if(profile)(witnesses??=new WeakMap()).set(result,{profile,sha:sha(result.output),bytes:Buffer.byteLength(result.output,'utf8'),metadata:JSON.stringify(result.metadata)})
  } catch { /* An optional view cannot fail successful tool work. */ }
  return result
}
export function transferBashView(from:object,to:Result):void {
  const p=witnesses?.get(from)
  if (!p)return
  try{if(eligible(to)&&p.sha===sha(to.output)&&p.bytes===Buffer.byteLength(to.output,'utf8')&&p.metadata===JSON.stringify(to.metadata))witnesses!.set(to,p)}catch{}
}
export function readBashView(value:object):Profile|undefined {
  const p=witnesses?.get(value);if(!p)return
  try{const v=value as Result;if(eligible(v)&&p.sha===sha(v.output)&&p.bytes===Buffer.byteLength(v.output,'utf8')&&p.metadata===JSON.stringify(v.metadata))return p.profile}catch{}
}
