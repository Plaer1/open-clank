import { filesFacadeClient } from '../filesFacadeClient.js';
import { wireDialog } from './overlays.js';
import { createResourcePicker } from './resourcePicker.js';

const conversionKey = accountId => `openclank.files.journal-conversion.${encodeURIComponent(accountId)}`;
const operationIdPattern = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i;
const receiptLabels = {
  applied:'Journal conversion applied',
  pending:'Journal conversion is pending final verification',
  needs_review:'Journal conversion needs review',
  rolled_back:'Journal conversion rolled back',
};
let activeManager;

function element(tag, text = '', className = '') {
  const node=document.createElement(tag);
  node.textContent=text;
  if (className) node.className=className;
  return node;
}

function button(text, action, {disabled=false, label=text}={}) {
  const node=element('button',text,'copal-btn');
  node.type='button';
  node.disabled=disabled;
  node.setAttribute('aria-label',label);
  node.addEventListener('click',action);
  return node;
}

function readScope(getScope) {
  const value=getScope?.();
  return {
    accountId:String(value?.accountId || ''),
    workspace:String(value?.workspace || ''),
    generation:Number(value?.generation || 0),
    epoch:Number(value?.epoch || 0),
  };
}

function sameScope(a,b) {
  return a.accountId===b.accountId && a.workspace===b.workspace && a.generation===b.generation && a.epoch===b.epoch;
}

function validCatalog(value, accountId) {
  return value?.schemaVersion===1 && value.accountId===accountId && Number.isSafeInteger(value.revision)
    && Array.isArray(value.configuredIds) && value.descriptors && typeof value.descriptors==='object';
}

/** Account-scoped Journal preferences, shared by the sidebar and Settings. */
export function openJournalManager({getScope,openJournal,onCatalogChanged}={}) {
  const scope=readScope(getScope);
  if (!scope.accountId || !scope.workspace) throw new Error('Sign in before managing Journals.');
  if (activeManager?.isCurrent() && sameScope(activeManager.scope,scope)) {
    activeManager.focus();
    return activeManager;
  }
  activeManager?.close();

  const controller=new AbortController(), {signal}=controller;
  const dialog=element('dialog','','copal-dialog copal-journal-manager');
  dialog.setAttribute('aria-label','Manage journals');
  const heading=element('h2','Manage journals');
  const list=element('div');
  const status=element('p','Loading journals…');
  status.setAttribute('role','status');
  const closeButton=button('Close',()=>manager.close());
  const isCurrent=()=>dialog.open && !signal.aborted && sameScope(scope,readScope(getScope));
  const focus=()=>dialog.querySelector('button:not(:disabled)')?.focus();
  let picker, child, catalog, busy=false;

  function setStatus(text) {
    if (!isCurrent()) return;
    status.textContent=text;
    status.hidden=!text;
  }

  function acceptCatalog(value,{changed=false}={}) {
    if (!isCurrent()) return;
    if (!validCatalog(value,scope.accountId) || catalog && value.revision<catalog.revision) {
      throw new Error('Journal catalog could not be verified for this account.');
    }
    catalog=value;
    if (changed) onCatalogChanged?.(catalog);
    if (changed) window.dispatchEvent(new CustomEvent('openclank:journal-catalog-changed',{
      detail:{accountId:scope.accountId,revision:catalog.revision},
    }));
    render();
  }

  async function loadCatalog() {
    acceptCatalog(await filesFacadeClient.binders({signal}));
  }

  async function run(action,{preserveStatus=false}={}) {
    if (!isCurrent() || busy) return;
    busy=true;
    if (!preserveStatus) setStatus('');
    render();
    try { await action(); }
    catch (error) {
      if (!isCurrent() || error?.name==='AbortError') return;
      if (error?.status===409) {
        try {
          await loadCatalog();
          setStatus('Journal configuration changed. Review the refreshed list and try again.');
        } catch {
          catalog=null;
          setStatus('Journal configuration changed and could not be refreshed. Retry to reload the list.');
        }
      } else setStatus(error?.message || 'The Journal action could not be completed.');
    } finally {
      busy=false;
      if (isCurrent()) {
        render();
        if (!dialog.contains(document.activeElement)) focus();
      }
    }
  }

  function describeChoice({title,description,confirmText,cancelText='Cancel',defaultValue=null,maxLength=80}) {
    if (!isCurrent()) return Promise.resolve(null);
    return new Promise(resolve=>{
      const prompt=child=element('dialog','','copal-dialog');
      prompt.setAttribute('aria-label',title);
      const form=element('form');
      form.method='dialog';
      const detail=element('p',description);
      detail.style.whiteSpace='pre-wrap';
      const controls=element('footer','','copal-dialog-actions');
      const cancel=button(cancelText,()=>prompt.close());
      const confirm=element('button',confirmText,'copal-btn primary');
      confirm.value='accept';
      if (defaultValue!==null) {
        const input=element('input');
        input.value=defaultValue;
        input.maxLength=maxLength;
        input.required=true;
        input.setAttribute('aria-label',title);
        form.append(input);
        prompt.addEventListener('close',()=>settle(prompt.returnValue==='accept' ? input.value.trim() : null),{once:true});
      } else prompt.addEventListener('close',()=>settle(prompt.returnValue==='accept'),{once:true});
      function settle(value) {
        if (child===prompt) child=null;
        resolve(isCurrent() ? value : null);
      }
      controls.append(cancel,confirm);
      form.append(controls);
      prompt.append(element('h2',title),detail,form);
      wireDialog(prompt);
      document.body.append(prompt);
      prompt.showModal();
    });
  }

  function pickFolder(rootLabel,current) {
    return new Promise(resolve=>{
      let selection=null;
      const opened=picker=createResourcePicker({
        client:filesFacadeClient,purpose:'folder',rootLabel,
        accountScope:scope.accountId,workspaceScope:scope.workspace,generation:scope.generation,
        getAccountScope:()=>readScope(getScope).accountId,getWorkspaceScope:()=>readScope(getScope).workspace,
        getGeneration:()=>readScope(getScope).generation,signal,isOriginCurrent:current,
        isCompatible:resource=>resource.provider==='copal' ? '' : 'Choose a folder from your account.',
        onSelect:resource=>{selection=resource;return true;},
        onClose:cancelled=>{
          if (picker===opened) picker=null;
          resolve(cancelled ? null : selection);
        },
      });
      void opened.open();
    });
  }

  function capture() {
    const before=catalog;
    return {before,current:()=>isCurrent() && catalog===before};
  }

  async function updateBinder(id,action) {
    const {before,current}=capture();
    const descriptor=before?.descriptors[id];
    if (!descriptor || !before.configuredIds.includes(id)) return;
    const proposed=structuredClone(before);
    if (action==='rename') {
      const name=await describeChoice({title:'Rename Journal',description:'Choose the Journal display name.',
        confirmText:'Save',defaultValue:descriptor.displayName});
      if (!name || name===descriptor.displayName || !current()) return;
      proposed.descriptors[id].displayName=name;
    } else if (action==='remove') {
      const confirmed=await describeChoice({title:'Remove Journal',confirmText:'Remove',
        description:`Remove “${descriptor.displayName}” from configured Journals? Its files remain in place.`});
      if (!confirmed || !current()) return;
      proposed.configuredIds=proposed.configuredIds.filter(item=>item!==id);
      delete proposed.descriptors[id];
      if (proposed.defaultId===id) proposed.defaultId=null;
    } else if (descriptor.enabled) proposed.defaultId=id;
    else return;
    if (!current()) return;
    const response=await fetch('/api/prefs/binder-catalog',{
      method:'PUT',credentials:'same-origin',headers:{'Content-Type':'application/json'},signal,
      body:JSON.stringify({expectedAccountId:before.accountId,expectedRevision:before.revision,catalog:proposed}),
    });
    if (!response.ok) throw Object.assign(new Error('Journal configuration could not be saved.'),{status:response.status});
    acceptCatalog(await response.json(),{changed:true});
  }

  async function addBinder() {
    const {before,current}=capture();
    if (!before) return;
    const source=await pickFolder('Choose a Journal folder',current);
    if (!source || !current()) return;
    const name=await describeChoice({title:'Add Journal',description:'Name this Journal.',
      confirmText:'Add',defaultValue:source.name || 'Journal'});
    if (!name || !current()) return;
    const value=await filesFacadeClient.configureBinder({
      resourceRef:source.ref,name,accountId:before.accountId,catalogRevision:before.revision,
      makeDefault:before.defaultId==null,
    },{signal});
    acceptCatalog(value,{changed:true});
  }

  function reportConversion(result,operationId) {
    const state=result?.receipt?.state;
    if (result?.operationId!==operationId || !Object.hasOwn(receiptLabels,state)) {
      throw new Error('Original conversion receipt could not be verified.');
    }
    setStatus(`${receiptLabels[state]}. Operation ${operationId}.`);
  }

  async function checkConversion() {
    let latestId='';
    try { latestId=localStorage.getItem(conversionKey(scope.accountId)) || ''; } catch {}
    const id=await describeChoice({title:'Check Journal conversion',description:'Enter the original conversion operation ID.',
      confirmText:'Check',defaultValue:latestId,maxLength:36});
    if (!id || !isCurrent()) return;
    if (!operationIdPattern.test(id)) throw new Error('Enter a valid Journal conversion operation ID.');
    try {
      const result=await filesFacadeClient.binderConversionReceipt(id,{signal});
      if (isCurrent()) reportConversion(result,id);
    } catch (error) {
      if (isCurrent()) throw new Error(`Original operation ${id} could not be verified. ${error.message || 'Receipt unavailable.'}`);
    }
  }

  async function reviewConversion() {
    const {before,current}=capture();
    if (!before) return;
    const source=await pickFolder('Choose the Journal source',current);
    if (!source || !current()) return;
    const untouched=await pickFolder('Choose the separate source to preserve',current);
    if (!untouched || !current()) return;
    const displayName=await describeChoice({title:'Preview Journal conversion',description:'Choose a display name for this Journal.',
      confirmText:'Preview',defaultValue:source.name || 'Journal'});
    if (!displayName || !current()) return;
    const binderId=`binder-${crypto.randomUUID()}`;
    setStatus('Preparing a read-only Journal preview…');
    const preview=await filesFacadeClient.previewBinder({sourceRef:source.ref,untouchedRef:untouched.ref,
      accountId:before.accountId,catalogRevision:before.revision,binderId,displayName},{signal});
    if (!current()) return;
    if (preview.accountId!==before.accountId || preview.proposal?.expectedRevision!==before.revision) {
      throw Object.assign(new Error('The account or catalog changed after review.'),{status:409});
    }
    const a=preview.sourceInventory,b=preview.untouchedInventory;
    const missing=a.missingIds.length+b.missingIds.length;
    const unresolved=a.references.unresolvedDigests.length+b.references.unresolvedDigests.length
      +a.references.unsupportedCount+b.references.unsupportedCount;
    const operationId=crypto.randomUUID();
    const summary=[
      `Account: ${preview.accountId}`, `Journal: ${displayName}`,
      `Selected source: ${JSON.stringify(preview.source)}`, `Separate source: ${JSON.stringify(preview.untouched)}`,
      `Catalog revision: ${preview.proposal.expectedRevision}`,
      `Before: ${preview.proposal.beforeDigest}`, `Proposed: ${preview.proposal.afterDigest}`,
      `Configured source fingerprint: ${preview.rootFingerprint}`,
      `Selected source fingerprint: ${a.fingerprint}`, `Separate source fingerprint: ${b.fingerprint}`,
      `Missing records: ${missing}. Unresolved or unsupported references: ${unresolved}.`,
      `Reference coverage: ${a.references.coverage}. External targets are unchecked.`,
      `Operation: ${operationId}`,
      missing ? 'Missing records block conversion.' : 'Applying makes the selected folder the only configured Journal and sets it as the default. Both sources stay in place.',
    ].join('\n');
    const confirmed=await describeChoice({title:'Review Journal conversion',description:summary,
      confirmText:missing ? 'Close' : 'Apply reviewed conversion'});
    if (!confirmed || missing || !current()) {if (current()) setStatus('');return;}
    const body={
      operation_id:operationId,source_ref:source.ref,untouched_ref:untouched.ref,
      expected_account_id:before.accountId,expected_catalog_revision:before.revision,
      binder_id:binderId,display_name:displayName,root_fingerprint:preview.rootFingerprint,
      source_fingerprint:a.fingerprint,untouched_fingerprint:b.fingerprint,catalog:preview.proposal.request,
    };
    // Preserve the receipt identifier even if the write response is lost.
    localStorage.setItem(conversionKey(before.accountId),operationId);
    setStatus(`Applying reviewed Journal conversion. Operation ${operationId}.`);
    let result;
    try { result=await filesFacadeClient.applyBinderConversion(body,{signal}); }
    catch (error) {
      if (!current()) return;
      try { result=await filesFacadeClient.binderConversionReceipt(operationId,{signal}); }
      catch {
        if (error?.status===409) throw error;
        throw new Error(`Original operation ${operationId} could not be verified. Use Check Journal conversion with this ID.`);
      }
    }
    if (!current()) return;
    reportConversion(result,operationId);
    acceptCatalog(await filesFacadeClient.binders({signal}),{changed:true});
  }

  function render() {
    const rows=[];
    for (const id of catalog?.configuredIds || []) {
      const descriptor=catalog.descriptors[id];
      if (!descriptor) continue;
      const name=descriptor.displayName;
      const isDefault=catalog.defaultId===id;
      const canOpen=descriptor.enabled && descriptor.source?.workspaceId===scope.workspace;
      const row=element('section');
      row.setAttribute('aria-label',name);
      const actions=element('div','','copal-dialog-actions');
      if (openJournal) actions.append(button('Open',()=>void run(async()=>{
        if (await openJournal(id) && isCurrent()) manager.close();
      }),{disabled:busy || !canOpen,label:`Open ${name}`}));
      actions.append(
        button('Rename',()=>void run(()=>updateBinder(id,'rename')),{disabled:busy,label:`Rename ${name}`}),
        button('Make default',()=>void run(()=>updateBinder(id,'default')),{disabled:busy || isDefault || !descriptor.enabled,label:`Make ${name} the default Journal`}),
        button('Remove',()=>void run(()=>updateBinder(id,'remove')),{disabled:busy,label:`Remove ${name}`}),
      );
      row.append(element('h3',name),...(isDefault ? [element('small','Default Journal')] : []),actions);
      rows.push(row);
    }
    if (catalog && !rows.length) rows.push(element('p','No journals configured.'));
    if (!catalog && !busy) rows.push(button('Retry',()=>void run(loadCatalog)));
    list.replaceChildren(...rows);
    add.disabled=busy || !catalog; preview.disabled=busy || !catalog; check.disabled=busy;
    dialog.setAttribute('aria-busy',String(busy));
  }

  const add=button('Add Journal…',()=>void run(addBinder));
  const preview=button('Review Journal conversion…',()=>void run(reviewConversion));
  const check=button('Check Journal conversion…',()=>void run(checkConversion));
  const footer=element('footer','','copal-dialog-actions');
  footer.append(add,preview,check,closeButton);
  dialog.append(heading,status,list,footer);
  const manager={scope,isCurrent,focus,close:()=>{if (dialog.open) dialog.close();}};
  activeManager=manager;
  dialog.addEventListener('close',()=>{
    controller.abort();
    picker?.destroy();
    child?.close();
    if (activeManager===manager) activeManager=null;
  },{once:true});
  for (const [target,event] of [[document,'openclank:auth-context-changed'],[window,'workspace-change'],[document,'openclank:file-policy-changed']]) {
    target.addEventListener(event,()=>manager.close(),{signal});
  }
  wireDialog(dialog);
  document.body.append(dialog);
  dialog.showModal();
  void run(loadCatalog,{preserveStatus:true}).then(()=>{
    if (catalog && status.textContent==='Loading journals…') setStatus('');
  });
  return manager;
}
