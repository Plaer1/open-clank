import type { EditorState } from '@codemirror/state';
import { syntaxTree, syntaxTreeAvailable } from '@codemirror/language';
import { parser as markdownParser } from '@lezer/markdown';

export interface SpellingToken { from:number; to:number; word:string }
export interface SpellingScope { tokens:SpellingToken[]; selection:boolean; truncated:boolean; skipped:number; message:string }
interface Span { from:number; to:number }
const CHAR_LIMIT = 32768, NODE_LIMIT = 4096, TOKEN_LIMIT = 2048;
const encoder = new TextEncoder();
const markdownOpaque = /^(?:FencedCode|CodeBlock|InlineCode|CodeText|CodeInfo|HTMLBlock|HTMLTag|Comment|URL|Autolink|LinkMark|ImageMark|Escape|Entity|SourceFrontmatter|SourceTemplate|SourceMath)$/;
const commentNode = /^(?:LineComment|BlockComment|Comment|DocComment|CommentBlock|comment)$|(?:^|_)comment(?:_|$)/i;

function subtract(span:Span, excluded:Span[]):Span[] {
  const result:Span[] = [];
  let from = span.from;
  for (const item of excluded.sort((a,b) => a.from-b.from)) {
    if (item.to <= from || item.from >= span.to) continue;
    if (item.from > from) result.push({ from, to:Math.min(item.from,span.to) });
    from = Math.max(from,item.to);
  }
  if (from < span.to) result.push({ from,to:span.to });
  return result;
}

/** Read only bounded captured ranges. Code is eligible solely through syntax
 * nodes; a missing/unknown grammar never becomes a whole-file prose heuristic. */
export function collectSpellingScope(state:EditorState, language:string, visible:readonly Span[], grammarReady:boolean):SpellingScope {
  const selection = state.selection.ranges.some(range => !range.empty);
  const result:SpellingScope = { tokens:[],selection,truncated:false,skipped:0,message:'' };
  const requested = selection ? state.selection.ranges.filter(range => !range.empty) : visible;
  const windows:Span[] = [];
  let remaining = CHAR_LIMIT;
  for (const range of requested.slice(0,16)) {
    if (!remaining) { result.truncated = true; break; }
    const to = Math.min(range.to,range.from+remaining);
    if (to > range.from) windows.push({ from:range.from,to });
    remaining -= to-range.from;
    if (to < range.to) result.truncated = true;
  }
  if (requested.length > 16) result.truncated = true;
  const tree = syntaxTree(state);
  const unknown = language === 'plaintext' || (grammarReady && tree.length === 0);
  if (unknown && !selection) { result.message = 'Select prose to check spelling in plain or unsupported syntax.'; return result; }
  if (!unknown && (!grammarReady || windows.some(range => !syntaxTreeAvailable(state,range.to)))) {
    result.message = 'Syntax is still loading for this range. Check a smaller selection when syntax is ready.'; return result;
  }
  let nodes = 0, parsedCharacters = 0;
  const eligible:Span[] = [];
  function prose(span:Span, sourceTree:any, offset=0, opaque:Span[] = []) {
    const excluded:Span[] = [...opaque];
    sourceTree.iterate({ from:span.from-offset,to:span.to-offset,enter(node:any) {
      if (++nodes > NODE_LIMIT) { result.truncated = true; excluded.push({ from:node.from+offset,to:span.to }); return false; }
      if (markdownOpaque.test(node.name)) { excluded.push({ from:node.from+offset,to:node.to+offset }); return false; }
    }});
    eligible.push(...subtract(span,excluded));
  }
  function commentProse(body:Span, window:Span, text?:string, opaque:Span[] = []) {
    // Parse only the certified body, never a second serialized source document.
    const length = body.to-body.from;
    if (length > CHAR_LIMIT-parsedCharacters) { result.truncated = true; return; }
    parsedCharacters += length;
    const clipped = { from:Math.max(body.from,window.from),to:Math.min(body.to,window.to) };
    if (clipped.to <= clipped.from) return;
    prose(clipped,markdownParser.parse(text ?? state.doc.sliceString(body.from,body.to)),body.from,opaque);
  }
  for (const window of windows) {
    if (unknown) { eligible.push(window); continue; }
    if (/^(?:markdown|mdx|rmarkdown)$/.test(language)) { prose(window,tree); continue; }
    const comments:Span[] = [];
    tree.iterate({ from:window.from,to:window.to,enter(node:any) {
      if (++nodes > NODE_LIMIT) { result.truncated = true; return false; }
      if (node.to-node.from > CHAR_LIMIT) return;
      if (commentNode.test(node.name)) {
        comments.push({ from:node.from,to:node.to }); return false;
      }
      // Conservative Python docstrings: one direct literal in the first
      // expression statement of a module/function/class body. No f/b strings,
      // concatenated strings or assignment values are inferred as prose.
      if (language === 'python' && node.name === 'ExpressionStatement') {
        const syntax = node.node, parent = syntax.parent;
        if (!(parent?.name === 'Script' || (parent?.name === 'Body' && /^(?:FunctionDefinition|ClassDefinition)$/.test(parent.parent?.name || '')))) return;
        for (let previous = syntax.prevSibling; previous; previous = previous.prevSibling) {
          if (++nodes > NODE_LIMIT) { result.truncated = true; return false; }
          if (/Statement$|^(?:FunctionDefinition|ClassDefinition|Decorated)$/.test(previous.name)) return;
        }
        const literal = syntax.firstChild;
        if (literal?.name !== 'String' || literal.nextSibling || literal.from !== syntax.from || literal.to !== syntax.to) return;
        const raw = state.doc.sliceString(literal.from,literal.to);
        const match = /^(?:[rRuU])?(\x22{3}|\x27{3}|\x22|\x27)/.exec(raw);
        if (!match || !raw.endsWith(match[1])) return;
        commentProse({ from:literal.from+match[0].length,to:literal.to-match[1].length },window); return false;
      }
    }});
    const groups:Array<{ from:number; to:number; parts:Span[] }> = [];
    for (const part of comments.sort((a,b) => a.from-b.from || a.to-b.to)) {
      const last = groups.at(-1);
      if (last && part.from >= last.to && part.to-last.from <= CHAR_LIMIT && /^\s*$/.test(state.doc.sliceString(last.to,part.from))) { last.to = part.to; last.parts.push(part); }
      else if (!last || part.from >= last.to) groups.push({ ...part,parts:[part] });
    }
    for (const group of groups) {
      if (group.to-group.from > CHAR_LIMIT-parsedCharacters) { result.truncated = true; continue; }
      let text = state.doc.sliceString(group.from,group.to);
      const patches:Span[] = [];
      for (const part of group.parts) {
        const raw = text.slice(part.from-group.from,part.to-group.from);
        const open = /^(?:\/\*+|\/\/+|<!--|#+|--+|;+|%+|REM\b)/i.exec(raw)?.[0].length || 0;
        const close = /(?:\*\/|-->)$/.exec(raw)?.[0].length || 0;
        patches.push({ from:part.from-group.from,to:part.from-group.from+open },{ from:part.to-group.from-close,to:part.to-group.from });
      }
      for (const match of text.matchAll(/^[ \t]*\*(?=\s|$)/gm)) patches.push({ from:match.index!,to:match.index!+match[0].length });
      const characters = text.split('');
      for (const patch of patches) for (let at=patch.from; at<patch.to; at++) characters[at] = ' ';
      text = characters.join('');
      commentProse(group,window,text,patches.map(patch => ({ from:group.from+patch.from,to:group.from+patch.to })));
    }
  }
  const seen = new Set<string>();
  for (const span of eligible) {
    const text = state.doc.sliceString(span.from,span.to);
    for (const chunk of text.matchAll(/\S+/g)) {
      // Skip paths, addresses, identifier-like tokens and numbers inside prose.
      if (/[\\/@_=\d]|\p{Ll}\p{Lu}/u.test(chunk[0])) { result.skipped++; continue; }
      for (const match of chunk[0].matchAll(/[\p{L}\p{M}]+(?:['’-][\p{L}\p{M}]+)*/gu)) {
        const word = match[0], from = span.from+chunk.index!+match.index!, to = from+word.length;
        if (encoder.encode(word).length > 64 || (/^\p{Lu}{2,}$/u.test(word))) { result.skipped++; continue; }
        // Do not diagnose a partial word at a clipped viewport/selection edge.
        if ((from === span.from && from > 0 && /[\p{L}\p{M}]/u.test(state.doc.sliceString(from-1,from))) || (to === span.to && to < state.doc.length && /[\p{L}\p{M}]/u.test(state.doc.sliceString(to,to+1)))) continue;
        const key = `${from}:${to}`;
        if (seen.has(key)) continue;
        if (result.tokens.length >= TOKEN_LIMIT) { result.truncated = true; return result; }
        seen.add(key); result.tokens.push({ from,to,word });
      }
    }
  }
  result.tokens.sort((a,b) => a.from-b.from || a.to-b.to);
  if (!result.tokens.length) result.message = 'No eligible prose words in the captured range.';
  return result;
}
