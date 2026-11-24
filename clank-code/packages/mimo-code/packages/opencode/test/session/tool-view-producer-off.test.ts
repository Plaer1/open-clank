import {expect,test} from 'bun:test'
import {bindToolViewProducer} from '../../src/session/tool-view-producer-admission'
import {markTerminalAnsi,readTerminalAnsi} from '../../src/session/tool-view-terminal-ansi'
import {markWebfetchHtml,readWebfetchHtml} from '../../src/tool/webfetch-view'
import {markStructuredSearch,readStructuredSearch} from '../../src/session/tool-view-structured-search'

test('producer profiles do no witness work without their live original On context',()=>{
 const off={}, stale={}, terminal={}, html={}, search={}
 let reads=0
 const untouched={get output(){reads++;return '\u001b[31mred\u001b[0m'},metadata:{exit:0,truncated:false}}
 markTerminalAnsi(untouched,off)
 markWebfetchHtml(untouched,{sessionID:'s',messageID:'m',callID:'c',requestedURL:'https://example.test',finalURL:'https://example.test'},off)
 markStructuredSearch(untouched,off)
 expect(reads).toBe(0)
 expect(readTerminalAnsi(untouched)).toBeUndefined();expect(readStructuredSearch(untouched)).toBe(false)
 bindToolViewProducer(stale,'terminal-ansi-v1',()=>false)
 markTerminalAnsi(untouched,stale);expect(reads).toBe(0)
 let current=true
 bindToolViewProducer(terminal,'terminal-ansi-v1',()=>current)
 const ansi={output:'\u001b[31mred\u001b[0m',metadata:{exit:0,truncated:false}}
 markTerminalAnsi(ansi,terminal);expect(readTerminalAnsi(ansi)).toBeDefined()
 const refused={output:'\u001b[31mred\u001b[0m',metadata:{exit:0,truncated:false}}
 current=false;markTerminalAnsi(refused,terminal);expect(readTerminalAnsi(refused)).toBeUndefined()
 const scope={sessionID:'s',messageID:'m',callID:'c',requestedURL:'https://example.test',finalURL:'https://example.test'}
 bindToolViewProducer(html,'html-markdown-v1',()=>true)
 const page={output:'<p>hello</p>',metadata:{truncated:false}}
 markWebfetchHtml(page,scope,html);expect(readWebfetchHtml(page,scope)).toBeDefined()
 bindToolViewProducer(search,'structured-search-v1',()=>true)
 const result={output:'{"status":"no_match","results":[]}',metadata:{truncated:false}}
 markStructuredSearch(result,search);expect(readStructuredSearch(result)).toBe(true)
 const wrong={output:result.output,metadata:{truncated:false}}
 markStructuredSearch(wrong,html);expect(readStructuredSearch(wrong)).toBe(false)
})
