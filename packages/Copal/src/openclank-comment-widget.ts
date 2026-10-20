import { WidgetType, type EditorView } from '@codemirror/view';
import { EditorSelection } from '@codemirror/state';
import { type SourceCommentRegion } from './openclank-comment-regions';

type BodyEditor = {destroy():void;focus?:()=>void;view?:EditorView};
type Capture = {expectedSource:string;expectedLocalRevision:number};
type PendingEdit = Capture & {id:string;body:string;flush:()=>{outcome:'queued'|'unchanged'|'failed';message?:string};discard:()=>void};
export interface CommentInsetOptions {
  render:(body:string,editBody?:(body:string)=>void)=>HTMLElement|null;
  createBodyEditor:(parent:HTMLElement,body:string,onChange:(body:string)=>void)=>BodyEditor;
  capture:(view:EditorView)=>Capture;
  apply:(view:EditorView,region:SourceCommentRegion,body:string,capture:Capture)=>{ok:boolean;error?:string};
  getPending?:(id:string)=>PendingEdit|undefined;
  registerPending?:(edit:PendingEdit)=>void;
  removePending?:(id:string)=>void;
  seeSource?:(region:SourceCommentRegion,event:Event)=>void;
  reveal?:(view:EditorView,region:SourceCommentRegion)=>void;
}
function regionEditId(region:SourceCommentRegion) {
  let hash=2166136261;for(let index=0;index<region.sourceText.length;index++)hash=Math.imul(hash^region.sourceText.charCodeAt(index),16777619);
  const occurrence=region.sourceDocument.sliceString(0,region.from).split(region.sourceText).length-1;
  return `comment:${region.languageId}:${(hash>>>0).toString(36)}:${occurrence}`;
}

/** Editable Markdown inset over an exact source region. Staging belongs to the
 * resource's dirty/recovery lifecycle; applying submits one guarded transaction.
 */
export class CommentInsetWidget extends WidgetType {
  private runtime={editor:null as BodyEditor|null,cleanup:()=>{}};
  private updateRegion:((region:SourceCommentRegion)=>void)|null=null;
  constructor(readonly region:SourceCommentRegion,readonly options:CommentInsetOptions){super();}
  eq(other:CommentInsetWidget){return this.region.from===other.region.from&&this.region.to===other.region.to&&this.region.sourceText===other.region.sourceText;}
  updateDOM(dom:HTMLElement,_view:EditorView,from:this){
    if(this.region.sourceText!==from.region.sourceText)return false;
    this.runtime=from.runtime;from.runtime={editor:null,cleanup:()=>{}};
    this.updateRegion=from.updateRegion;this.updateRegion?.(this.region);
    return true;
  }
  toDOM(view:EditorView){
    let region=this.region;
    const runtime=this.runtime,editId=regionEditId(region);
    let pending=this.options.getPending?.(editId);
    let capture:Capture=pending||this.options.capture(view);
    let body=pending?.body??region.body,editing=false;
    const root=document.createElement(region.sourceText.includes('\n')?'div':'span');
    root.className=`cm-rich-comment-widget${region.sourceText.includes('\n')?' cm-rich-comment-block':''}`;
    root.dataset.commentSourceFrom=String(region.from);root.dataset.commentSourceTo=String(region.to);
    this.updateRegion=next=>{region=next;root.dataset.commentSourceFrom=String(next.from);root.dataset.commentSourceTo=String(next.to);};
    root.setAttribute('aria-label',region.kind==='docstring'?'Editable rich docstring':'Editable rich comment');root.tabIndex=0;
    const content=document.createElement('span');content.className='cm-rich-comment-prose';
    const editorHost=document.createElement('div');editorHost.className='cm-rich-comment-body-edit';editorHost.hidden=true;
    const notice=document.createElement('p');notice.className='cm-rich-comment-notice';notice.setAttribute('role','status');notice.hidden=true;
    const preview=()=>{let node:HTMLElement|null=null;try{node=this.options.render(body,value=>{if(!region.editable||!this.options.registerPending)return;stage(value);commit();});}catch{}if(node)content.replaceChildren(node);else content.textContent=body;};
    const closeEditor=()=>{runtime.editor?.destroy();runtime.editor=null;editorHost.replaceChildren();editorHost.hidden=true;content.hidden=false;editing=false;root.classList.remove('cm-rich-comment-editing');};
    const applyBody=()=>{
      const result=this.options.apply(view,region,body,capture);
      if(result.ok){this.options.removePending?.(editId);pending=undefined;closeEditor();preview();notice.hidden=true;}
      else {if(!editing)preview();notice.textContent=result.error||'This body cannot be applied safely. Correct it here or use source mode; it remains unsaved.';notice.hidden=false;}
      return {outcome:result.ok?'queued' as const:'failed' as const,message:result.error};
    };
    const register=()=>{
      pending={id:editId,body,...capture,flush:applyBody,discard:()=>{body=region.body;pending=undefined;closeEditor();preview();notice.hidden=true;}};
      this.options.registerPending?.(pending);
    };
    const stage=(value:string)=>{
      if(!pending)capture=this.options.capture(view);
      body=value;notice.hidden=true;
      if(body===region.body){this.options.removePending?.(editId);pending=undefined;}else register();
      view.requestMeasure();
    };
    const commit=()=>{
      if(pending)return applyBody();
      closeEditor();preview();view.requestMeasure();
      return {outcome:'unchanged' as const};
    };
    const activate=(event?:MouseEvent)=>{
      if(editing)return;
      if(!region.editable||!this.options.registerPending){notice.textContent=region.error||'This comment is editable in source mode.';notice.hidden=false;return;}
      if(!pending)capture=this.options.capture(view);
      editing=true;content.hidden=true;editorHost.hidden=false;root.classList.add('cm-rich-comment-editing');
      runtime.editor=this.options.createBodyEditor(editorHost,body,stage);
      runtime.editor.focus?.();runtime.editor.view?.focus();
      if(event&&runtime.editor.view){const position=runtime.editor.view.posAtCoords({x:event.clientX,y:event.clientY});if(position!=null)runtime.editor.view.dispatch({selection:EditorSelection.cursor(position)});}
      view.requestMeasure();
    };
    preview();root.append(content,editorHost,notice);
    root.addEventListener('click',rawEvent=>{
      const event=rawEvent as MouseEvent;
      if(editing||(event.target as Element)?.closest?.('a,button,input,select,textarea,.copal-attachment,.copal-media-embed'))return;
      event.preventDefault();event.stopPropagation();activate(event);
    });
    root.addEventListener('keydown',rawEvent=>{const event=rawEvent as KeyboardEvent;if(event.target===root&&(event.key==='Enter'||event.key==='F2')){event.preventDefault();activate();}});
    root.addEventListener('focusout',()=>queueMicrotask(()=>{if(editing&&root.isConnected&&!root.contains(root.ownerDocument.activeElement))commit();}));
    const outside=(event:Event)=>{if(editing&&!root.contains(event.target as Node))commit();};
    root.ownerDocument.addEventListener('pointerdown',outside,true);
    runtime.cleanup=()=>root.ownerDocument.removeEventListener('pointerdown',outside,true);
    root.addEventListener('contextmenu',event=>this.options.seeSource?.(region,event));
    // Reattach recovered bodies and expose them in place without another box.
    if(pending){register();queueMicrotask(()=>{if(root.isConnected)activate();});}
    return root;
  }

  destroy(){this.runtime.cleanup();this.runtime.editor?.destroy();this.runtime.editor=null;}
  ignoreEvent(){return true;}
}
