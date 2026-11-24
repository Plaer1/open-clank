/** Render only bounded ASCII carriage-return overwrites, not a terminal emulator. */
export function progressRedraw(input: string): { text: string; overwrittenCharacters: number; redrawEvents: number } | undefined {
  if (typeof input !== "string" || input.length > 51200) return
  let line: string[] = []
  const lines: string[] = []
  let column = 0, overwrittenCharacters = 0, redrawEvents = 0
  let redrawSegment = false, segmentOverwrote = false
  const finishSegment = () => {
    if (redrawSegment && segmentOverwrote) redrawEvents++
    redrawSegment = false
    segmentOverwrote = false
  }
  for (let i = 0; i < input.length; i++) {
    const ch = input.charCodeAt(i)
    if (ch === 13) {
      finishSegment()
      if (input.charCodeAt(i + 1) === 10) {
        lines.push(line.join("") + "\r\n")
        line = []; column = 0; i++
      } else {
        column = 0; redrawSegment = true
      }
    } else if (ch === 10) {
      finishSegment()
      lines.push(line.join("") + "\n")
      line = []; column = 0
    } else if (ch >= 32 && ch <= 126) {
      if (column < line.length) { overwrittenCharacters++; segmentOverwrote = true }
      line[column++] = input[i]
    } else return
  }
  finishSegment()
  const text = lines.join("") + line.join("")
  if (!overwrittenCharacters || text.length >= input.length) return
  return { text, overwrittenCharacters, redrawEvents }
}
