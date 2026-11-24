/** Comment projections derived from the mounted syntax tree and its language data.
 * No delimiter search can promote ordinary source text into a comment.
 */
import type { EditorState } from '@codemirror/state';
import { syntaxTree, syntaxTreeAvailable } from '@codemirror/language';
import { getStyleTags, tags } from '@lezer/highlight';
import type { SyntaxNode, SyntaxNodeRef } from '@lezer/common';
import { sourceLanguageMetadata, sourceCommentCapability, type SourceCommentForm } from './openclank-source-languages';
import { docstringRegionFromExpression, isPythonStatementName, type DocumentationRegion } from './openclank-doc-regions';

export interface CommentBodySpan { from:number; to:number; bodyFrom:number; bodyTo:number }
export interface SourceCommentRegion extends DocumentationRegion {
  languageId:string;
  sourceDocument:EditorState['doc'];
  sourceText:string;
  body:string;
  bodySpans:CommentBodySpan[];
  form:SourceCommentForm;
  editable:boolean;
  error?:string;
}
type Window = {from:number;to:number};
const commentTag = (node:SyntaxNodeRef) => /comment/i.test(node.name) || getStyleTags(node)?.tags.some(tag=>tag.set.includes(tags.comment)) === true;

function formsAt(state:EditorState, position:number):SourceCommentForm[] {
  const custom = state.languageDataAt<SourceCommentForm[]>('richCommentForms',position).flat();
  const standard = state.languageDataAt<any>('commentTokens',position).flatMap(tokens=>[
    ...(tokens.line ? [{open:tokens.line}] : []),...(tokens.block ? [tokens.block] : []),
  ]);
  return [...custom,...standard].filter(form=>form?.open).sort((a,b)=>b.open.length-a.open.length);
}

function matchingForm(text:string, forms:SourceCommentForm[]):SourceCommentForm | null {
  // Generalized long-bracket metadata describes the tokenizer's delimiter family.
  for (const form of forms) {
    if (form.dynamic === 'lua-long' || form.dynamic === 'cmake-bracket') {
      const match = (form.dynamic === 'lua-long' ? /^--\[(=*)\[/ : /^#\[(=*)\[/).exec(text);
      if (match) return {...form,open:match[0],close:`]${match[1]}]`};
    }
    if (form.caseInsensitive ? text.toLowerCase().startsWith(form.open.toLowerCase()) : text.startsWith(form.open)) {
      // Documentation variants share the parser's comment token and base form.
      if (form.open === '//' && /^(?:\/\/\/|\/\/!)/.test(text)) return {...form,open:text.slice(0,3)};
      if (form.open === '/*' && /^(?:\/\*\*|\/\*!)/.test(text) && !text.startsWith('/**/')) return {...form,open:text.slice(0,3)};
      return {...form,open:text.slice(0,form.open.length)};
    }
  }
  return null;
}

function mappedRegion(state:EditorState, source:string, region:DocumentationRegion, form:SourceCommentForm, languageId:string):SourceCommentRegion {
  let bodyOffset = 0;
  const bodySpans = region.contentRanges.map((span,index)=>{
    const mapped = {...span,bodyFrom:bodyOffset,bodyTo:bodyOffset+span.to-span.from};
    bodyOffset=mapped.bodyTo+(index+1<region.contentRanges.length?1:0);return mapped;
  });
  const body=region.contentRanges.map(span=>source.slice(span.from,span.to)).join('\n');
  const docstringCompound = region.kind==='docstring' && (!region.close || !/^([rRuU]*)(?:"""|'''|"|')$/.test(region.open));
  return {...region,languageId,sourceDocument:state.doc,sourceText:source.slice(region.from,region.to),body,bodySpans,form,
    editable:!!form.open&&!docstringCompound,
    ...(!form.open ? {error:'This syntax tokenizer identified a comment but did not expose its wrapper. Edit its source.'}
      :docstringCompound?{error:'Adjacent or parenthesized docstring segments retain their exact source. Edit their source to change the body.'}:{}),
  };
}

function commentRegion(state:EditorState, source:string, from:number, to:number, form:SourceCommentForm, languageId:string):SourceCommentRegion {
  const contentRanges:Array<{from:number;to:number}> = [];
  const contentFrom=from+form.open.length,contentTo=form.close?to-form.close.length:to;
  let cursor=contentFrom;
  while (cursor<=contentTo) {
    const newline=source.indexOf('\n',cursor);
    let end=newline<0||newline>=contentTo?contentTo:newline;
    if (end>cursor&&source[end-1]==='\r') end--;
    let start=cursor;
    if (cursor===contentFrom) { if (source[start]===' ') start++; }
    else if (form.close) {
      const indentation=/^[ \t]*/.exec(source.slice(start,end))![0];start+=indentation.length;
      if (form.open.startsWith('/*') && source[start]==='*' && (start+1>=end || /[ \t]/.test(source[start+1]))) {start++;if(source[start]===' ')start++;}
    }
    if (form.close&&end===contentTo&&source[end-1]===' ') end--;
    contentRanges.push({from:Math.min(start,end),to:end});
    if (newline<0||newline>=contentTo)break;
    cursor=newline+1;
  }
  const lineStart=source.lastIndexOf('\n',from-1)+1;
  const nextBreak=source.indexOf('\n',to);
  const language=sourceLanguageMetadata(languageId).displayName;
  return mappedRegion(state,source,{from,to,contentFrom,contentTo,contentRanges,language,
    delimiterKind:form.close?'block':'line',open:form.open,close:form.close,kind:'comment',lineStart,lineEnd:nextBreak<0?source.length:nextBreak},form,languageId);
}

function pythonRegion(state:EditorState, source:string, expression:SyntaxNode):SourceCommentRegion | null {
  const parent=expression.parent;
  if (!parent||!(parent.name==='Script'||(parent.name==='Body'&&/^(ClassDefinition|FunctionDefinition)$/.test(parent.parent?.name||''))))return null;
  for(let previous=expression.prevSibling;previous;previous=previous.prevSibling)if(isPythonStatementName(previous.name))return null;
  const nodes:Array<{name:string;from:number;to:number}>=[];
  expression.cursor().iterate(node=>{nodes.push({name:node.name,from:node.from,to:node.to});});
  const region=docstringRegionFromExpression(source,{name:expression.name,from:expression.from,to:expression.to},nodes);
  return region?mappedRegion(state,source,region,{open:region.open,close:region.close},'python'):null;
}

/** Visible occurrences plus their actual adjacent tokenizer spans. No reparsing.
 * A stream token in the middle of a block walks its comment siblings to recover
 * its opener; the same mechanism handles comments beyond the old 32KiB cutoff.
 */
export function sourceCommentRegions(state:EditorState, language:string, windows:readonly Window[], source=state.doc.toString()):SourceCommentRegion[] {
  const entry=sourceLanguageMetadata(language);
  if(sourceCommentCapability(entry.id).strategy==='none'||!windows.length)return [];
  const tree=syntaxTree(state);
  const spans=new Map<string,{from:number;to:number;node:SyntaxNode}>();
  const docstrings=new Map<string,SourceCommentRegion>();
  const include=(node:SyntaxNode)=>spans.set(`${node.from}:${node.to}`,{from:node.from,to:node.to,node});
  for(const window of windows)tree.iterate({from:window.from,to:window.to,enter(ref){
    if(entry.id==='python'&&ref.name==='ExpressionStatement') {
      const region=pythonRegion(state,source,ref.node);if(region)docstrings.set(`${region.from}:${region.to}`,region);
    }
    if(!commentTag(ref)||ref.from===ref.to)return;
    if(spans.has(`${ref.from}:${ref.to}`))return false;
    include(ref.node);
    // Stream trees keep styled tokens as siblings. Grammar comments are already
    // complete nodes, and only genuine comment siblings can extend the discovery.
    for(let previous=ref.node.prevSibling,edge=ref.from;previous&&commentTag(previous)&&/^\s*$/.test(source.slice(previous.to,edge));previous=previous.prevSibling){include(previous);edge=previous.from;}
    for(let next=ref.node.nextSibling,edge=ref.to;next&&commentTag(next)&&/^\s*$/.test(source.slice(edge,next.from));next=next.nextSibling){include(next);edge=next.to;}
    return false;
  }});
  let enclosingEnd=-1;
  const ordered=[...spans.values()].sort((a,b)=>a.from-b.from||b.to-a.to).filter(span=>{if(span.to<=enclosingEnd)return false;enclosingEnd=span.to;return true;});
  const regions:SourceCommentRegion[]=[];
  for(let index=0;index<ordered.length;index++) {
    const span=ordered[index];
    const dataMarkers=state.languageDataAt<string[]>('richCommentDataMarkers',span.from).flat();
    if(dataMarkers.some(marker=>source.slice(span.from,span.to).startsWith(marker)))break;
    const forms=formsAt(state,span.from);
    const raw=source.slice(span.from,span.to);
    const form=matchingForm(raw,forms)||(/LineComment/.test(span.node.name)&&/^#[^#]/.test(raw)?{open:'#'}:null);
    let to=span.to;
    if(form?.close) {
      while(!source.slice(span.from,to).endsWith(form.close)&&index+1<ordered.length&&/^\s*$/.test(source.slice(to,ordered[index+1].from)))to=ordered[++index].to;
      // An incomplete parse or unclosed comment must keep source visible.
      if(!source.slice(span.from,to).endsWith(form.close))continue;
    }
    // A parser-backed comment can reveal a missing form (e.g. an alternative
    // upstream delimiter). Render it honestly; never guess an editable wrapper.
    const region=commentRegion(state,source,span.from,to,form||{open:''},entry.id);
    const previous=regions.at(-1);
    const gap=previous?source.slice(previous.to,region.from):'';
    if(previous&&!form?.close&&previous.form.open&&((previous.form.open===region.form.open)||(!form&&previous.form.indented))&&/^(?:\r?\n[ \t]*)+$/.test(gap)) {
      if(!form&&previous.form.indented) {
        region.contentRanges=region.contentRanges.map(bodySpan=>({...bodySpan,from:bodySpan.from+(/^[ \t]*/.exec(source.slice(bodySpan.from,bodySpan.to))![0].length)}));
      }
      const emptyLines:Array<{from:number;to:number}>=[];
      for(let at=source.indexOf('\n',previous.to)+1;at>0&&at<region.lineStart;at=source.indexOf('\n',at)+1){emptyLines.push({from:at,to:at});if(source.indexOf('\n',at)<0)break;}
      const grouped={...previous,to:region.to,contentTo:region.contentTo,lineEnd:region.lineEnd,contentRanges:[...previous.contentRanges,...emptyLines,...region.contentRanges]};
      regions[regions.length-1]=mappedRegion(state,source,grouped,previous.form,entry.id);
    } else regions.push(region);
  }
  return [...regions,...docstrings.values()].filter(region=>windows.some(window=>region.to>=window.from&&region.from<=window.to)).sort((a,b)=>a.from-b.from);
}

export type CommentBodyChange = {ok:true;change:{from:number;to:number;insert:string};expectedSource:string}|{ok:false;error:string};

/** Preserve wrappers and source outside this region; refuse unrepresentable bodies.
 * Returned changes are submitted once to the resource-owned transaction/history.
 */
export function serializeCommentBody(source:string, region:SourceCommentRegion, nextBody:string):CommentBodyChange {
  if(source.slice(region.from,region.to)!==region.sourceText)return {ok:false,error:'This comment changed while you were editing. Review its current source before applying the body.'};
  if(!region.editable)return {ok:false,error:region.error||'Edit this comment in source mode.'};
  const body=String(nextBody).replace(/\r\n?|\n/g,'\n');
  const form=region.form;
  if(form.close&&form.nested) {
    let depth=0;
    for(let at=0;at<body.length;at++) {
      if(body.startsWith(form.open,at)){depth++;at+=form.open.length-1;}
      else if(body.startsWith(form.close,at)){if(--depth<0)return {ok:false,error:'The body closes its outer comment delimiter. Keep editing or use source mode.'};at+=form.close.length-1;}
    }
    if(depth)return {ok:false,error:'The body contains an unmatched nested comment delimiter. Keep editing or use source mode.'};
  } else {
    if(form.close&&body.includes(form.close))return {ok:false,error:`The body contains the closing delimiter ${form.close}. Use source mode or change that text before applying.`};
    if(form.close&&body.includes(form.open))return {ok:false,error:`The body contains the opening delimiter ${form.open}. Use source mode to preserve its nesting.`};
  }
  if(form.indented&&/^[\w-]+::/.test(body))return {ok:false,error:'This text would turn the comment into a directive. Keep editing or use source mode.'};
  if(region.kind==='docstring') {
    const raw=/^[rR]/.test(region.open);
    if(!raw&&/\\/.test(body))return {ok:false,error:'Backslashes change Python string values. Edit this docstring in source mode to choose its exact escaping.'};
    if(raw&&/\\$/.test(body))return {ok:false,error:'A raw Python string cannot end in an unmatched backslash. Keep editing or use source mode.'};
    if(form.close?.length===1&&body.includes('\n'))return {ok:false,error:'This single-line docstring cannot contain a newline without changing its string syntax. Use source mode.'};
  }
  const newline=region.sourceText.includes('\r\n')?'\r\n':'\n';
  const lines=body.split('\n');
  const spans=region.contentRanges;
  if(!spans.length)return {ok:false,error:'This comment has no editable body span.'};
  let insert='';
  if(region.kind==='docstring')insert=region.open+body.split('\n').join(newline)+(region.close||'');
  else if(!form.close) {
    const indent=/^[ \t]*/.exec(source.slice(region.lineStart,region.from))![0];
    const prefixes=spans.map(span=>source.slice(source.lastIndexOf('\n',span.from-1)+1,span.from));
    insert=lines.map((line,index)=>{
      let prefix=index===0?source.slice(region.from,spans[0].from):prefixes[index]||`${indent}${form.indented?'   ':form.open+' '}`;
      if(/[A-Za-z]$/.test(prefix))prefix+=' ';
      return prefix+line;
    }).join(newline);
  } else {
    const prefix=source.slice(region.from,spans[0].from);
    const suffix=source.slice(spans.at(-1)!.to,region.to);
    const existingPrefixes=spans.slice(1).map(span=>source.slice(source.lastIndexOf('\n',span.from-1)+1,span.from));
    const indent=/^[ \t]*/.exec(source.slice(region.lineStart,region.from))![0];
    const continuation=existingPrefixes.find(value=>value.trim())||`${indent}${form.open.startsWith('/*')?' * ':''}`;
    let rebuilt=lines.map((line,index)=>index===0?line:(existingPrefixes[index-1]??continuation)+line).join(newline);
    if(form.lineDelimited&&!/^(?:\s|$)/.test(rebuilt))rebuilt=' '+rebuilt;
    if(form.lineDelimited&&!rebuilt.endsWith(newline))rebuilt+=newline;
    insert=prefix+rebuilt+suffix;
  }
  return {ok:true,change:{from:region.from,to:region.to,insert},expectedSource:region.sourceText};
}

/** Exact UTF-16 source/body mapping for selection and structured inline edits. */
export function commentBodySourceOffset(region:SourceCommentRegion, offset:number, side:-1|1=1) {
  const at=Math.max(0,Math.min(offset,region.body.length));
  for(let index=0;index<region.bodySpans.length;index++) {
    const span=region.bodySpans[index];
    if(at<=span.bodyTo)return span.from+Math.max(0,at-span.bodyFrom);
    if(at<span.bodyTo+1)return side<0?span.to:region.bodySpans[index+1]?.from??span.to;
  }
  return region.contentTo;
}

/** Attachment insertion uses the same mounted syntax authority as presentation. */
export function safeSourceCommentInsertion(state:EditorState,language:string,offset:number) {
  const source=state.doc.toString();const at=Math.max(0,Math.min(offset,source.length));
  const line=state.doc.lineAt(at),indent=/^[ \t]*/.exec(line.text)![0];
  if(!syntaxTreeAvailable(state,line.to))return {ok:false as const,error:'Syntax is still loading. Wait before inserting a comment.'};
  const inside=sourceCommentRegions(state,language,[{from:line.from,to:line.to}],source).find(region=>at>region.from&&at<region.to);
  if(inside) {
    if(!inside.contentRanges.some(span=>at>=span.from&&at<=span.to))return {ok:false as const,error:'Place the cursor inside the comment body, outside its delimiters.'};
    return {ok:true as const,from:at,to:at,indent,comment:'',text:''};
  }
  for(let node:SyntaxNode|null=syntaxTree(state).resolveInner(line.to,-1);node;node=node.parent) {
    if(node.from<=line.to&&line.to<node.to&&(/String|Template|Heredoc|Comment/.test(node.name)||getStyleTags(node)?.tags.some(tag=>tag.set.includes(tags.string)||tag.set.includes(tags.comment))))return {ok:false as const,error:'That line boundary is inside a string or comment. Move outside it before inserting.'};
  }
  const form=formsAt(state,line.to).find(form=>!form.close)||formsAt(state,line.to)[0];
  if(!form)return {ok:false as const,error:'This syntax has no usable comment form; insert the image in a Markdown document instead.'};
  return {ok:true as const,from:line.to,to:line.to,indent,comment:form.open,text:''};
}

export function wrapSourceComment(state:EditorState,position:number,body:string,indent='') {
  const forms=formsAt(state,position),form=forms.find(value=>!value.close)||forms[0];
  if(!form)return {ok:false as const,error:'This syntax has no usable comment form.'};
  if(form.close&&(body.includes(form.close)||body.includes(form.open)))return {ok:false as const,error:'This embed contains a comment delimiter. Edit its source to choose a safe representation.'};
  const lines=String(body).split('\n');
  return {ok:true as const,text:form.close?`${indent}${form.open}\n${lines.map(line=>indent+line).join('\n')}\n${indent}${form.close}`:lines.map(line=>`${indent}${form.open} ${line}`).join('\n')};
}
