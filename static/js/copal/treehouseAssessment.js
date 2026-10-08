import { registerTreeHouseView } from './treehouseViews.js';
import { filesFacadeClient } from '../filesFacadeClient.js';
import { mountEditorFilesBrowser, filesBrowserResource } from './editorFilesBrowser.js';
import { wireDialog } from './overlays.js';

const field = (h, title, control) => { if(control.tagName==='TEXTAREA'&&control.hasAttribute('value'))control.value=control.getAttribute('value'); return h('label', {class:'th-author-field'}, h('span', {text:title}), control); };
const button = (h, title, run, primary=false) => h('button', {type:'button',class:`copal-btn${primary ? ' primary' : ''}`,text:title,onclick:run});
function dialog(ctx, title) {
  const {h} = ctx, form = h('form',{class:'th-assessment-form'}), error=h('p',{role:'alert'});
  const modal=h('dialog',{class:'copal-dialog th-author-dialog'},h('h2',{text:title}),form,error);
  document.body.append(modal); wireDialog(modal); modal.addEventListener('close',()=>modal.remove());modal.showModal();
  const run=async action=>{try {error.textContent='';const result=await action();if(result)modal.close();}catch(e){error.textContent=e.message;}};
  return {form,modal,error,run};
}
export function openTreeHouseSubmission(assignment, ctx) {
  const {h,ui,command}=ctx, state=ui.snapshot.state;
  const submission=state.submissions[`${assignment.id}:${ui.actorId}`], saved=submission?.draftAnswer ?? (submission?.status !== 'reset' ? submission?.answer : null);
  const {form,modal,run}=dialog(ctx,assignment.title), kind=assignment.assessmentType || 'text';
  const prompt=h('div',{class:'copal-markdown th-assessment-prompt'},ctx.renderMarkdown(assignment.prompt||''));form.append(prompt);
  let answer;
  if(kind==='quiz') {
    const choices=new Map();
    for(const q of assignment.questions || []) {
      const group=h('fieldset',{},h('legend',{text:q.prompt})); const controls=[];
      for(const option of q.options) {const input=h('input',{type:'checkbox',value:option.id,checked:saved?.[q.id]?.includes(option.id)});controls.push(input);group.append(field(h,option.text,input));}
      choices.set(q.id,controls);form.append(group);
    }
    answer=()=>Object.fromEntries([...choices].map(([id,controls])=>[id,controls.filter(x=>x.checked).map(x=>x.value)]));
  } else if(kind==='file') {
    const receipts=structuredClone(saved?.fileReceipts||[]),selected=h('div');
    const draw=()=>{selected.replaceChildren(...receipts.map((receipt,index)=>h('p',{},h('span',{text:receipt.name}),button(h,'Remove',()=>{receipts.splice(index,1);draw();}))));};draw();
    form.append(h('p',{text:'Select your work from Files. Upload a local file through the Files browser if needed; every selection is prepared for this account and task before submission.'}),selected,button(h,'Select or upload file…',async()=>{
      const host=h('div',{class:'th-files-modal-host'}),status=h('p',{role:'alert'}),picker=h('dialog',{class:'copal-dialog th-files-modal'},h('h2',{text:'Choose submission file'}),host,status,button(h,'Close',()=>picker.close()));let browser;
      document.body.append(picker);wireDialog(picker);picker.addEventListener('close',()=>{browser?.dispose();picker.remove();});picker.showModal();
      try{const roots=await filesFacadeClient.roots({copalWorkspace:ui.snapshot.workspace});
        browser=await mountEditorFilesBrowser({container:host,getLayoutScope:()=>({owner:ui.snapshot.accountId,workspace:ui.snapshot.workspace,surface:'editor'}),onConfirm:async row=>{try{const resource=filesBrowserResource(row),grant=Object.values(state.courseGrants||{}).find(x=>x.courseId===assignment.courseId&&x.recipientId===ui.actorId&&!x.revokedAt);
          const prepared=await filesFacadeClient.prepareAttachment({operationId:`treehouse-work-${crypto.randomUUID()}`,generation:roots.generation,workspace:ui.snapshot.workspace,mode:'link',source:{resource_ref:resource.ref,expected_revision:resource.revision},target:{kind:'treehouse_submission',courseId:assignment.courseId,assignmentId:assignment.id,expectedRevision:{kind:'treehouse',value:JSON.stringify({grantRevision:grant?.revision||0,catalogueRevision:state.courses[assignment.courseId]?.curriculumRevision??state.revision})}}});
          receipts.push({operationId:prepared.operation_id,preparationReceiptId:prepared.preparation_receipt_id,name:prepared.insertion.label});draw();picker.close();return true;
        }catch(e){status.textContent=e.message;return false;}}},()=>picker.isConnected);await browser?.open();
      }catch(e){status.textContent=e.message;}
    }));
    answer=()=>({fileReceipts:receipts});
  } else {const text=h('textarea',{rows:12,'aria-label':'Your response',value:typeof saved==='string'?saved:''});form.append(field(h,'Your response',text));answer=()=>text.value;}
  form.append(h('p',{text:`${assignment.graded===false?'Ungraded work':`Pass criterion: ${assignment.passPercent||0}% · ${assignment.maxPoints} maximum points`}${assignment.dueAt ? ` · Due ${new Date(assignment.dueAt).toLocaleString()}` : ' · No deadline'} · ${assignment.maxAttempts ? `${assignment.maxAttempts} attempts maximum` : 'Unlimited attempts'}`}));
  if(submission?.attemptHistory?.length) {
    const history=h('details',{},h('summary',{text:`Attempt history (${submission.attemptHistory.length})`}));
    for(const attempt of submission.attemptHistory)history.append(h('p',{text:`Attempt ${attempt.ordinal} · ${new Date(attempt.submittedAt).toLocaleString()} · generation ${attempt.resetGeneration}`}));form.append(history);
  }
  if(submission?.feedback)form.append(h('aside',{class:'th-feedback'},h('strong',{text:`${submission.status} · ${submission.grade ?? '—'}/${assignment.maxPoints}`}),h('p',{text:submission.feedback}),...Object.entries(submission.criterionFeedback||{}).map(([id,text])=>h('p',{text:`${assignment.rubric?.find(x=>x.id===id)?.title || id}: ${text}`}))));
  const generation=ctx.learnerProjection().courses?.[assignment.courseId]?.resetGeneration||0;
  form.append(h('footer',{class:'copal-dialog-actions'},button(h,'Cancel',()=>modal.close()),button(h,'Save draft',()=>run(()=>command('submission.draft',{assignmentId:assignment.id,answer:answer(),generation}))),button(h,'Submit attempt',()=>run(()=>command('submission.submit',{assignmentId:assignment.id,answer:answer(),generation})),true)));
  form.onsubmit=event=>event.preventDefault();
}
export function openTreeHouseReview(submission, ctx) {
  const {h,ui,command}=ctx, assignment=ui.snapshot.state.assignments[submission.assignmentId];
  const {form,modal,run}=dialog(ctx,`Review · ${assignment.title}`);
  form.append(h('p',{text:`Attempt ${submission.attempts} · ${submission.status}`}),h('pre',{class:'th-submission-answer',text:typeof submission.answer==='string'?submission.answer:JSON.stringify(submission.answer,null,2)}));
  if(assignment.assessmentType==='file')for(const file of submission.answer?.fileReceipts||[]) {
    const path=`/api/copal/treehouse/courses/${encodeURIComponent(assignment.courseId)}/submissions/${encodeURIComponent(submission.id)}/files/${encodeURIComponent(file.operationId)}?attemptId=${encodeURIComponent(submission.attemptId)}&workspace=${encodeURIComponent(ui.snapshot.workspace||'default')}`;
    form.append(h('a',{href:path,target:'_blank',rel:'noopener',text:`Download submitted file · ${file.name||'File evidence'}`}));
  }
  if(assignment.assessmentType==='file')form.append(h('p',{text:'Downloads use the saved source revision. If the file changed or access expired, the submitted receipt remains in history; ask the learner to prepare a new attempt.'}));
  const score=h('input',{type:'number',min:0,max:assignment.maxPoints,value:submission.grade??0}),feedback=h('textarea',{rows:5,value:submission.feedback||''}),reason=h('textarea',{rows:3,required:submission.status==='graded'}),criteria={};
  form.append(field(h,`Score (0–${assignment.maxPoints})`,score),field(h,'Feedback to learner',feedback));
  for(const criterion of assignment.rubric||[]) {const control=h('textarea',{rows:3,value:submission.criterionFeedback?.[criterion.id]||''});criteria[criterion.id]=control;form.append(field(h,criterion.title,control));}
  if(submission.status==='graded')form.append(field(h,'Reason for grade correction (required)',reason));
  if(submission.reviewHistory?.length)form.append(h('details',{},h('summary',{text:'Previous reviews'}),...submission.reviewHistory.map(review=>h('p',{text:`${review.reviewedAt} · ${review.score} points · ${review.correctionReason||review.feedback||'Quiz key'}`}))));
  form.append(h('footer',{class:'copal-dialog-actions'},button(h,'Cancel',()=>modal.close()),button(h,submission.status==='graded'?'Save grade correction':'Review and grade',()=>run(()=>command('submission.grade',{submissionId:submission.id,courseId:assignment.courseId,curriculumRevision:ui.snapshot.state.courses[assignment.courseId]?.curriculumRevision,attemptId:submission.attemptId,score:Number(score.value),feedback:feedback.value,criterionFeedback:Object.fromEntries(Object.entries(criteria).map(([id,control])=>[id,control.value])),correctionReason:reason.value})),true)));
  form.onsubmit=e=>e.preventDefault();
}
export function openTreeHouseAssessmentAuthor(module, ctx, assignment=null) {
  const {h,command}=ctx,{form,modal,run}=dialog(ctx,assignment?'Edit assessment':'Add assessment');
  const input=(type,value='')=>h('input',{type,value});
  const title=input('text',assignment?.title||''),prompt=h('textarea',{rows:5,value:assignment?.prompt||''});
  const kind=h('select',{},...[['text','Text task'],['file','Prepared file task'],['quiz','Choice quiz']].map(([value,text])=>h('option',{value,text})));kind.value=assignment?.assessmentType||'text';
  const graded=input('checkbox');graded.checked=assignment?.graded!==false;
  const maximum=input('number',assignment?.maxPoints||100),pass=input('number',assignment?.passPercent||0),attempts=input('number',assignment?.maxAttempts||0),due=input('datetime-local',assignment?.dueAt?.slice(0,16)||''),available=input('datetime-local',assignment?.availableAt?.slice(0,16)||''),until=input('datetime-local',assignment?.availableUntil?.slice(0,16)||'');
  const objectives=h('textarea',{rows:3,value:(assignment?.objectives||[]).join('\n')}),rubric=h('textarea',{rows:3,value:(assignment?.rubric||[]).map(x=>x.title).join('\n')});
  form.append(field(h,'Title',title),field(h,'Task type',kind),field(h,'Instructions',prompt),field(h,'Objectives (one per line)',objectives),field(h,'Feedback criteria (one per line)',rubric),field(h,'Graded work',graded),field(h,'Maximum points',maximum),field(h,'Passing percentage',pass),field(h,'Maximum attempts (0 = unlimited)',attempts),field(h,'Optional deadline',due),field(h,'Available from (optional)',available),field(h,'Available until (optional)',until));
  const questionHost=h('section',{class:'th-quiz-author'}),questions=structuredClone(assignment?.questions||[]);
  const draw=()=>{questionHost.replaceChildren();if(kind.value!=='quiz'){questionHost.hidden=true;return;}questionHost.hidden=false;
    questions.forEach((q,index)=>{const question=h('input',{value:q.prompt,'aria-label':`Question ${index+1}`,oninput:e=>q.prompt=e.target.value});const group=h('fieldset',{},h('legend',{text:`Question ${index+1}`}),question);
      q.options.forEach(option=>{const correct=h('input',{type:'checkbox',checked:q.correctOptionIds.includes(option.id),onchange:e=>{q.correctOptionIds=e.target.checked?[...new Set([...q.correctOptionIds,option.id])]:q.correctOptionIds.filter(x=>x!==option.id);}}),text=h('input',{value:option.text,'aria-label':'Option text',oninput:e=>option.text=e.target.value});group.append(field(h,'Correct answer',correct),text);});
      group.append(button(h,'Add option',()=>{q.options.push({id:`option-${crypto.randomUUID()}`,text:''});draw();}),button(h,'Remove question',()=>{questions.splice(index,1);draw();}));questionHost.append(group);});
    questionHost.append(button(h,'Add question',()=>{questions.push({id:`question-${crypto.randomUUID()}`,prompt:'',options:[{id:'a',text:''},{id:'b',text:''}],correctOptionIds:['a']});draw();}));};kind.onchange=draw;draw();form.append(questionHost);
  const save=async()=>{const payload={moduleId:module.id,title:title.value,prompt:prompt.value,assessmentType:kind.value,graded:graded.checked,maxPoints:Number(maximum.value),passPercent:Number(pass.value),maxAttempts:Number(attempts.value),allowRetries:true,dueAt:due.value?new Date(due.value).toISOString():'',availableAt:available.value?new Date(available.value).toISOString():'',availableUntil:until.value?new Date(until.value).toISOString():'',objectives:objectives.value.split('\n').map(x=>x.trim()).filter(Boolean),rubric:rubric.value.split('\n').map(x=>x.trim()).filter(Boolean).map((text,i)=>({id:assignment?.rubric?.[i]?.id||`criterion-${crypto.randomUUID()}`,title:text})),questions:kind.value==='quiz'?questions:[]};if(assignment)payload.assignmentId=assignment.id;return command(assignment?'assignment.update':'assignment.create',payload);};
  form.append(h('footer',{class:'copal-dialog-actions'},button(h,'Cancel',()=>modal.close()),button(h,'Save draft',()=>run(save),true)));form.onsubmit=e=>e.preventDefault();
}
registerTreeHouseView('assignments',(root,ctx)=>{
  const {h,ui}=ctx,state=ui.snapshot.state;
  root.append(h('h1',{text:'Tasks and feedback'}));
  for(const assignment of Object.values(state.assignments)) {
    const course=state.courses[assignment.courseId],submission=state.submissions[`${assignment.id}:${ui.actorId}`];
    const card=h('article',{class:'th-assessment-card'},h('h2',{text:assignment.title}),h('p',{text:`${course?.title||''} · ${assignment.assessmentType||'text'} · ${assignment.status} · ${assignment.graded===false?'Ungraded':`Pass ${assignment.passPercent||0}%`}`}));
    if(ctx.adminMode()&&ctx.courseCanEdit(assignment.courseId)) {card.append(button(h,'Edit task',()=>openTreeHouseAssessmentAuthor(state.modules[assignment.moduleId],ctx,assignment)));if(assignment.status==='draft')card.append(button(h,'Publish task',()=>ctx.command('assignment.publish',{assignmentId:assignment.id})));for(const attempt of Object.values(state.submissions).filter(x=>x.assignmentId===assignment.id&&['submitted','graded'].includes(x.status)))card.append(button(h,`${attempt.profileId} · ${attempt.status} · attempt ${attempt.attempts}`,()=>openTreeHouseReview(attempt,ctx)));}
    else {card.append(h('p',{text:submission?.status==='graded'?`Reviewed: ${submission.grade}/${assignment.maxPoints} · ${submission.feedback}`:submission?.status||'No work submitted'}),button(h,submission?'Open work and feedback':'Start task',()=>openTreeHouseSubmission(assignment,ctx)));}
    root.append(card);
  }
});
