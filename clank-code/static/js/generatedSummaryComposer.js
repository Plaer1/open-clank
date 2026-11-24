/** Stage only the host-issued attachment into the exact captured draft. */
export function stageGeneratedSummaryCAS(receipt,{expected,read,account,isCurrent=()=>true,enqueue}) {
  const scopeCurrent=()=>{const now=read();return !!expected && now.sid===expected.sid && now.epoch===expected.epoch && now.authEpoch===expected.authEpoch && now.owner===expected.owner && account()===expected.owner}
  const current=()=>{const now=read();return isCurrent() && scopeCurrent() && now.version===expected.version && now.text===expected.text && now.attachments===expected.attachments && Array.isArray(now.pending) && Array.isArray(expected.pending) && now.pending.length===expected.pending.length && now.pending.every((item,index)=>item===expected.pending[index])}
  return !!expected && receipt?.sessionId===expected.sid && enqueue(receipt,current,scopeCurrent)===true
}
