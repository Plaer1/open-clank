/** Actual MCP adapter boundary: no user metadata can supply this private admission. */
export function recordMcpJsonCandidate(input:{callID:string; output:string; normalizedOutput:string; gcfOn:boolean;
  toonCurrent?:()=>boolean; outline?:{sidecar:string;current:()=>boolean;text:string;toolCallID:string}; aborted:boolean; truncated:boolean; attachmentCount:number; isError:boolean;
  content:readonly {type:string}[]; hasStructuredContent:boolean;
  record:(id:string, output:string, toonCurrent?:()=>boolean, outline?:{sidecar:string;current:()=>boolean})=>void}):void {
  const toon = !input.hasStructuredContent && input.content.every(item=>item.type==='text') ? input.toonCurrent : undefined
  const attempted=input.outline?.toolCallID===input.callID ? input.outline : undefined
  const outline=attempted ? {current:attempted.current,sidecar:!input.hasStructuredContent && input.content.length===1 && input.content[0]?.type==='text' && attempted.text===input.output && attempted.current()===true ? attempted.sidecar : 'null'} : undefined
  if ((input.gcfOn || toon?.()===true || !!outline) && !input.aborted && !input.truncated && !input.isError && input.attachmentCount===0 &&
      input.normalizedOutput===input.output) input.record(input.callID,input.output,toon,outline)
}
