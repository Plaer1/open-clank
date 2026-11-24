/** Strict, bounded single TAP 13 document; output is a review body, never a TAP transcript. */
export function summarizeTap13(input:string):{body:string;omitted:number}|undefined {
  if (Buffer.byteLength(input,'utf8')>51200 || /[\x00-\x09\x0b-\x1f\x7f`<>]/.test(input) || !input.endsWith('\n')) return
  const lines=input.slice(0,-1).split('\n')
  if (lines.length>4098 || lines[0]!=='TAP version 13') return
  let plan:number|undefined, expected=1, omitted=0, failed=false, sawAssertion=false, afterPlan=false
  const kept=[lines[0]!]
  for (const line of lines.slice(1)) {
    const p=/^1\.\.([1-9][0-9]*)$/.exec(line)
    if (p) {
      if (plan!==undefined || Number(p[1])>4096) return
      plan=Number(p[1]); afterPlan=sawAssertion; kept.push(line); failed=false; continue
    }
    const a=/^(not ok|ok) ([1-9][0-9]*)(?: - ([^#]*))?$/.exec(line)
    if (a) {
      if (afterPlan || Number(a[2])!==expected++ || expected>4097) return
      sawAssertion=true; failed=a[1]==='not ok'
      if (failed) kept.push(line); else omitted++
      continue
    }
    if (failed && /^# [^#]*$/.test(line)) { kept.push(line); continue }
    return
  }
  if (plan===undefined || expected-1!==plan || omitted===0) return
  return {body:kept.join('\n')+'\n',omitted}
}
