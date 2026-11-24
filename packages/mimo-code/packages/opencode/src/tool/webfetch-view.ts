/** Private webfetch evidence and conservative, optional HTML model view. */
import { createHash } from 'node:crypto'
import { toolViewProducerCurrent } from '../session/tool-view-producer-admission'
import TurndownService from 'turndown'

type Result = {output:string; metadata?:Record<string,unknown>; attachments?:unknown[]}
export type WebfetchHtmlScope = Readonly<{sessionID:string;messageID:string;callID:string;requestedURL:string;finalURL:string}>
type Witness = WebfetchHtmlScope & Readonly<{version:1;mime:'text/html';outputSHA256:string;outputBytes:number}>
type MarkdownWitness = WebfetchHtmlScope & Readonly<{version:1;mime:'text/markdown'|'text/x-markdown'|'text/html';outputSHA256:string;outputBytes:number}>
let witnesses: WeakMap<object,Witness> | undefined
let markdownWitnesses: WeakMap<object,MarkdownWitness> | undefined
const sha = (s:string) => createHash('sha256').update(s,'utf8').digest('hex')
const validText = (s:string) => !/[\uD800-\uDFFF]/u.test(s) && Buffer.byteLength(s,'utf8') <= 51200
const eligible = (result:Result) => typeof result.output==='string' && validText(result.output) &&
  !result.attachments?.length && result.metadata?.truncated !== true && result.metadata?.outputPath === undefined

/** Called only by the native webfetch owner after response and redirect validation. */
export function markWebfetchHtml<T extends Result>(result:T, scope:WebfetchHtmlScope, context:object):T {
  if (!toolViewProducerCurrent(context,'html-markdown-v1') || !eligible(result) || Object.values(scope).some(v=>typeof v!=='string' || !v || Buffer.byteLength(v,'utf8')>2048)) return result
  ;(witnesses ??= new WeakMap()).set(result,Object.freeze({...scope,version:1,mime:'text/html',outputSHA256:sha(result.output),outputBytes:Buffer.byteLength(result.output,'utf8')}))
  return result
}
/** Mint only at the actual requested-Markdown producer after URL and MIME validation. */
export function markWebfetchMarkdown<T extends Result>(result:T, scope:WebfetchHtmlScope, mime:MarkdownWitness['mime'], context:object):T {
  if (!toolViewProducerCurrent(context,'markdown-slim-v1') || !eligible(result) || Object.values(scope).some(v=>typeof v!=='string' || !v || Buffer.byteLength(v,'utf8')>2048)) return result
  ;(markdownWitnesses ??= new WeakMap()).set(result,Object.freeze({...scope,version:1,mime,outputSHA256:sha(result.output),outputBytes:Buffer.byteLength(result.output,'utf8')}))
  return result
}
export function readWebfetchMarkdown(result:Result, scope:Pick<WebfetchHtmlScope,'sessionID'|'messageID'|'callID'>):MarkdownWitness | undefined {
  const proof=markdownWitnesses?.get(result)
  return proof && eligible(result) && proof.sessionID===scope.sessionID && proof.messageID===scope.messageID && proof.callID===scope.callID &&
    sha(result.output)===proof.outputSHA256 && Buffer.byteLength(result.output,'utf8')===proof.outputBytes ? proof : undefined
}
/** Transfer only across an owned wrapper after truncation/plugins, never via metadata. */
export function transferWebfetchHtml(from:Result,to:Result):void {
  const md=markdownWitnesses?.get(from)
  if (md && eligible(to) && from.output===to.output && sha(to.output)===md.outputSHA256 && Buffer.byteLength(to.output,'utf8')===md.outputBytes) markdownWitnesses!.set(to,md)
  const proof=witnesses?.get(from)
  if (proof && eligible(to) && from.output===to.output && sha(to.output)===proof.outputSHA256 && Buffer.byteLength(to.output,'utf8')===proof.outputBytes)
    witnesses!.set(to,proof)
}
export function readWebfetchHtml(result:Result, scope:Pick<WebfetchHtmlScope,'sessionID'|'messageID'|'callID'>):Witness | undefined {
  const proof=witnesses?.get(result)
  return proof && eligible(result) && proof.sessionID===scope.sessionID && proof.messageID===scope.messageID && proof.callID===scope.callID &&
    sha(result.output)===proof.outputSHA256 && Buffer.byteLength(result.output,'utf8')===proof.outputBytes ? proof : undefined
}

/** Existing webfetch Markdown representation. */
export function convertHTMLToMarkdown(html:string):string {
  const service=new TurndownService({headingStyle:'atx',hr:'---',bulletListMarker:'-',codeBlockStyle:'fenced',emDelimiter:'*'})
  service.remove(['script','style','meta','link'])
  return service.turndown(html)
}
/** Only simple prose markup is admitted. Protected structures and attributes stay raw. */
export function framedHtmlView(input:string, ref:{messageID:string;partID:string;revision:number;contentHash:string}):string | undefined {
  if (!validText(input) || !Number.isSafeInteger(ref.revision) || ref.revision<1 || !/^[a-f0-9]{64}$/.test(ref.contentHash) ||
      [ref.messageID,ref.partID].some(x=>typeof x!=='string'||!x||x.length>256)) return
  // No attributes, comments, entities, declarations, links, code or structured markup:
  // malformed/ambiguous HTML and protected spans are deliberately not converted.
  const tags=input.match(/<[^>]*>/g) ?? []
  if (input.replace(/<[^>]*>/g,'').includes('<') || input.replace(/<[^>]*>/g,'').includes('>') || input.includes('&') ||
      tags.some(tag=>!/^<\/?(?:html|body|main|article|section|div|p|h[1-6]|ul|ol|li|blockquote|strong|em|b|i|br)\s*\/?\s*>$/i.test(tag))) return
  const stack:string[]=[]
  for (const tag of tags) {
    const match=/^<(\/?)([a-z0-9]+)\s*(\/?)\s*>$/i.exec(tag)
    if (!match) return
    const name=match[2]!.toLowerCase()
    if (name==='br') { if (match[1]) return; continue }
    if (match[3]) return
    if (match[1]) { if (stack.pop()!==name) return }
    else stack.push(name)
    if (stack.length>128) return
  }
  if (stack.length) return
  let body:string
  try { body=convertHTMLToMarkdown(input) } catch { return }
  const invocation=JSON.stringify({operation:'exact_output',message_id:ref.messageID,part_id:ref.partID,revision:ref.revision,content_hash:ref.contentHash,byte_offset:0,byte_limit:8192})
  const frame=`[Lossy HTML-to-Markdown view; markup and formatting may be omitted. Original: history(${invocation}); follow nextOffset for further pages.]\n${body}`
  return Buffer.byteLength(frame,'utf8') < Buffer.byteLength(input,'utf8') ? frame : undefined
}
