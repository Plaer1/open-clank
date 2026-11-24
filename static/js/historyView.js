import { createOpenClankWindow } from './copal/windows.js';
import { createHistoryChangeSets } from './historyChangeSets.js';
import { folderChangeLabel, validFolderChanges } from './historyFolderModel.js';

const PAGE_LIMIT = 50;
const READ_CHUNK_BYTES = 64 * 1024;
const PREVIEW_LIMIT_BYTES = 256 * 1024;

function element(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'text') node.textContent = String(value ?? '');
    else if (key === 'class') node.className = String(value ?? '');
    else if (key === 'onclick' && typeof value === 'function') node.addEventListener('click', value);
    else if (typeof value === 'boolean') { if (value) node.setAttribute(key, ''); }
    else if (value != null) node.setAttribute(key, String(value));
  }
  for (const child of children.flat()) {
    if (child == null) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

function actionButton(label, action, { disabled = false, focusKey = '' } = {}) {
  const button = element('button', {
    type: 'button',
    class: 'copal-btn',
    text: label,
    onclick: action,
    ...(focusKey ? { 'data-history-focus': focusKey } : {}),
  });
  button.disabled = disabled;
  return button;
}

function millisLabel(value) {
  const timestamp = Number(value);
  if (!Number.isFinite(timestamp) || timestamp <= 0) return 'Time unavailable';
  const date = new Date(timestamp);
  return Number.isNaN(date.getTime()) ? 'Time unavailable' : date.toLocaleString();
}

function descriptorLabel(version, fallback) {
  const operation = String(version?.operation || 'change').replaceAll('_', ' ');
  const phase = String(version?.phase || 'version').replaceAll('_', ' ');
  const state = String(version?.action_state || 'state unavailable').replaceAll('_', ' ');
  return operation + ' · ' + phase + ' · ' + state;
}

class ResourceHistoryView {
  constructor() {
    this.generation = 0;
    this.impactPending = null;
    this.target = null;
    this.pendingRestoreVerification = null;
    this.items = [];
    this.nextCursor = null;
    this.pageCursors = [null];
    this.pageIndex = 0;
    this.selectedRef = '';
    this.selectedVersion = null;
    this.listBusy = false;
    this.listError = '';
    this.previewBusy = false;
    this.previewError = '';
    this.previewText = '';
    this.previewNote = '';
    this.restorePreview = null;
    this.restorePreviewBusy = false;
    this.restoring = false;
    this.contextInvalid = false;
    this.statusMessage = '';
    this.statusBad = false;
    this.listController = null;
    this.changeController = null;
    this.originController = null;
    this.observedStepController = null;
    this.observedStep = null;
    this.capturedActionLocator = null;
    this.capturedGroup = null;
    this.changeCompareController = null;
    this.changeComparison = null;
    this.checkpointComparison = null;
    this.checkpointController = null;
    this.checkpointRestore = null;
    this.checkpointRestorePending = null;
    this.regularRestore = null;
    this.regularRestoreAccounts = new Map();
    this.regularRestoreController = null;
    this.changes = null;
    this.managedChanges = null;
    this.originFragments = null;
    this.changeComparison = null;
    this.previewController = null;
    this.restoreController = null;
    this.restorePreviewController = null;
    this.previewGeneration = 0;
    this.window = createOpenClankWindow({
      id: 'history-window',
      label: 'History',
      subtitle: 'Files · Version history',
      minWidth: 720,
      minHeight: 520,
      className: 'history-resource-window',
      onBeforeClose: () => {
        if (this.restoring) {
          this.statusMessage = 'A restore is in progress for the selected file.';
          this.statusBad = true;
          this.render();
          return false;
        }
        this.reset();
        return true;
      },
      onClosed: () => this.reset(),
    });
    const invalidateFolder = () => { if (this.folder && !this.restoring) this.render(); };
    document.addEventListener('openclank:auth-context-changed', invalidateFolder);
    window.addEventListener('openclank:auth-context-changed', invalidateFolder);
    document.addEventListener('openclank:file-policy-changed', invalidateFolder);
    document.addEventListener('odysseus:session-selected', invalidateFolder);
    window.addEventListener('workspace-change', invalidateFolder);
  }

  get regularRestorePending() { return this.regularRestoreAccounts.get(document.body.dataset.accountId)?.pending || null; }
  set regularRestorePending(value) {
    const account=document.body.dataset.accountId, prior=this.regularRestoreAccounts.get(account) || {};
    this.regularRestoreAccounts.set(account,{...prior,pending:value});
  }
  get regularRestoreResult() { return this.regularRestoreAccounts.get(document.body.dataset.accountId)?.result || null; }
  set regularRestoreResult(value) {
    const account=document.body.dataset.accountId, prior=this.regularRestoreAccounts.get(account) || {};
    this.regularRestoreAccounts.set(account,{...prior,result:value});
  }
  checkpointPendingForAccount() { return this.checkpointRestorePending?.account===document.body.dataset.accountId; }

  reset() {
    this.changeSets?.clearAction();
    this.generation += 1;
    this.impactPending = null;
    this.cancelReads();
    this.target = null;
    this.folder = null;
    this.changes = null;
    this.managedChanges = null;
    this.originFragments = null;
    this.observedStep = null;
    this.capturedActionLocator = null;
    this.capturedGroup = null;
    this.pendingRestoreVerification = null;
    this.checkpointComparison = null;
    this.checkpointRestore = null;
    this.regularRestore = null;
    this.items = [];
    this.nextCursor = null;
    this.pageCursors = [null];
    this.pageIndex = 0;
    this.selectedRef = '';
    this.selectedVersion = null;
    this.listBusy = false;
    this.listError = '';
    this.previewBusy = false;
    this.previewError = '';
    this.previewText = '';
    this.previewNote = '';
    this.restorePreview = null;
    this.restorePreviewBusy = false;
    this.contextInvalid = false;
    this.statusMessage = '';
    this.statusBad = false;
  }

  cancelReads() {
    this.folderController?.abort();
    this.folderController = null;
    this.regularRestoreController?.abort();
    this.regularRestoreController = null;
    this.observedStepController?.abort();
    this.observedStepController = null;
    this.checkpointController?.abort();
    this.checkpointController = null;
    this.originController?.abort();
    this.originController = null;
    this.changeController?.abort();
    this.changeController = null;
    this.changeCompareController?.abort();
    this.changeCompareController = null;
    this.listController?.abort();
    this.previewController?.abort();
    this.restoreController?.abort();
    this.restorePreviewController?.abort();
    this.listController = null;
    this.previewController = null;
    this.restoreController = null;
    this.restorePreviewController = null;
  }

  open(input = {}, trigger = null) {
    if (this.restoring) {
      this.statusMessage = 'A restore is in progress for the selected file. Wait for it to finish before changing History targets.';
      this.statusBad = true;
      this.render();
      return false;
    }
    const resourceId = String(input.resourceId || '').trim();
    if (!resourceId) {
      this.window.setStatus('This resource has no registered History identity.', true);
      return false;
    }
    this.cancelReads();
    this.folder = null;
    this.generation += 1;
    this.impactPending = null;
    if(input.changeSetContext){
      if(this.changeSets)this.changeSets.bind(input.changeSetContext);
      else this.changeSets=createHistoryChangeSets({context:input.changeSetContext,render:()=>this.render(),comparator:locator=>{const token=this.generation;void resolveCapturedChange(locator,()=>this.guardContext(token)).then(data=>{if(this.guardContext(token))this.openCapturedAction(data,{locator});}).catch(()=>{if(this.guardContext(token)){this.statusMessage='Captured change unavailable.';this.render();}});}});
    } else this.changeSets?.clearAction();
    this.target = {
      resourceId,
      name: String(input.name || 'Selected file'),
      provider: String(input.provider || ''),
      isContextCurrent: typeof input.isContextCurrent === 'function' ? input.isContextCurrent : null,
      isStatusCurrent: typeof input.isStatusCurrent === 'function' ? input.isStatusCurrent : null,
      onRestored: typeof input.onRestored === 'function' ? input.onRestored : null,
      onRecordedImpact: typeof input.onRecordedImpact === 'function' ? input.onRecordedImpact : null,
      onSelectVersion: typeof input.onSelectVersion === 'function' ? input.onSelectVersion : null,
      exactOnly: input.exactOnly === true,
    };
    this.changes = null;
    this.managedChanges = null;
    this.originFragments = null;
    this.observedStep = null;
    this.capturedActionLocator = null;
    this.capturedGroup = null;
    this.changeComparison = null;
    this.checkpointComparison = null;
    this.checkpointRestore = null;
    this.regularRestore = null;
    this.items = [];
    this.nextCursor = null;
    this.pageCursors = [null];
    this.pageIndex = 0;
    this.selectedRef = '';
    this.selectedVersion = null;
    this.listBusy = false;
    this.listError = '';
    this.previewBusy = false;
    this.previewError = '';
    this.previewText = '';
    this.previewNote = '';
    this.restorePreview = null;
    this.restorePreviewBusy = false;
    this.contextInvalid = false;
    this.statusMessage = '';
    this.statusBad = false;
    this.window.setTitle('History · ' + this.target.name);
    this.window.setSubtitle('Files · Version history');
    this.window.show(trigger || document.activeElement);
    this.render();
    if (!this.target.exactOnly) void this.loadPage(null);
    if (input.managedGroups === true) void this.loadManagedChanges();
    return true;
  }

  openFolder(input = {}, trigger = null, removedOnly = false) {
    if (this.restoring) return false;
    const folderRef = String(input.folderRef || '').trim();
    if (!folderRef || typeof input.isContextCurrent !== 'function' || !input.isContextCurrent()) return false;
    this.reset();
    const opener = input.opener || trigger || document.activeElement;
    this.folder = { ...input, folderRef, name: String(input.name || 'Folder'), opener, removedOnly, items: [], error: '', busy: true, next: null };
    this.window.setTitle('History · ' + this.folder.name);
    this.window.setSubtitle(removedOnly ? 'Removed files' : 'Folder changes');
    this.window.show(opener);
    void this.loadFolderPage(0);
    return true;
  }

  async loadFolderPage(cursor) {
    const folder = this.folder;
    if (!folder?.isContextCurrent()) { this.render(); return; }
    this.folderController?.abort();
    const controller = new AbortController();
    this.folderController = controller;
    const current = () => this.folder === folder && this.folderController === controller;
    folder.busy = true;
    folder.error = '';
    folder.cursor = cursor;
    this.render();
    try {
      const path = folder.removedOnly ? 'removed-files' : 'folder-changes';
      const params = new URLSearchParams({ limit: '25', cursor: String(cursor), folder_ref: folder.folderRef });
      const value = await this.requestJson('/api/history/resources/' + path + '?' + params, { signal: controller.signal });
      if (!current() || !folder.isContextCurrent()) return;
      const ok = folder.removedOnly
        ? value?.version === 1 && value.scope === 'one_open_folder' && Array.isArray(value.items) && value.items.length <= 25
          && value.items.every(item => typeof item?.name === 'string' && /^resource-[0-9a-f]{64}$/.test(item.resource_id || '')
            && Number.isSafeInteger(item.size_bytes ?? 0) && Number.isSafeInteger(item.removed_at_ms ?? 0))
        : validFolderChanges(value);
      if (!ok) throw new Error('invalid');
      folder.items = cursor ? folder.items.concat(value.items) : value.items;
      folder.next = Number.isSafeInteger(value.next_cursor) && value.next_cursor > cursor ? value.next_cursor : null;
    } catch {
      if (current()) folder.error = folder.removedOnly ? 'Removed files are unavailable.' : 'Folder changes are unavailable.';
    } finally {
      if (current()) {
        this.folderController = null;
        folder.busy = false;
        this.render();
      }
    }
  }

  openFolderVersion(folder, item) {
    if (this.folder !== folder || !folder.isContextCurrent()) { this.render(); return; }
    const target = {
      resourceId: item.resource_id,
      name: item.name,
      provider: 'host',
      isContextCurrent: folder.isContextCurrent,
      isStatusCurrent: folder.isContextCurrent,
      onRestored: folder.onRestored,
    };
    if (this.open(target, folder.opener)) {
      this.folder = folder;
      this.render();
    }
  }

  isCurrent(token, versionRef = null) {
    return token === this.generation
      && this.target != null
      && (versionRef == null || this.selectedRef === versionRef);
  }

  contextMatches() {
    if (this.contextInvalid || !this.target) return false;
    if (!this.target.isContextCurrent) return true;
    try { return this.target.isContextCurrent() !== false; }
    catch (_) { return false; }
  }

  guardContext(token) {
    if (!this.isCurrent(token)) return false;
    if (this.contextMatches()) return true;
    this.contextInvalid = true;
    this.originController?.abort();
    this.regularRestoreController?.abort();
    this.observedStepController?.abort();
    this.changeController?.abort();
    this.listController?.abort();
    this.previewController?.abort();
    this.restorePreviewController?.abort();
    this.statusMessage = 'The Files or Editor target changed. Reopen History from the resource you want to review.';
    this.statusBad = true;
    this.render();
    return false;
  }

  async requestJson(url, options = {}) {
    const response = await fetch(url, { credentials: 'same-origin', ...options, cache: 'no-store' });
    let payload = null;
    const text = await response.text();
    if (text) {
      try { payload = JSON.parse(text); }
      catch (_) {
        if (response.ok) throw new Error('History returned an unreadable response.');
      }
    }
    if (!response.ok) {
      const detail = payload?.detail || payload?.message || payload?.error || '';
      const error = new Error(String((detail && typeof detail === 'object' ? detail.message : detail) || response.statusText || 'History request failed.'));
      error.status = response.status;
      throw error;
    }
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
      throw new Error('History returned an invalid response.');
    }
    return payload;
  }

  versionsUrl(cursor = null) {
    const params = new URLSearchParams({ limit: String(PAGE_LIMIT) });
    if (cursor) params.set('cursor', cursor);
    return '/api/history/resources/' + encodeURIComponent(this.target.resourceId) + '/versions?' + params.toString();
  }

  async loadChanges(cursor = null) {
    const token=this.generation;
    if (!this.guardContext(token)) return;
    this.changeController?.abort(); this.changeCompareController?.abort(); this.changeComparison=null;
    const controller=new AbortController(); this.changeController=controller;
    this.changes={items:[],next:null,busy:true,error:''}; this.render();
    try {
      const params=new URLSearchParams({limit:String(PAGE_LIMIT)}); if(cursor)params.set('cursor',cursor);
      const data=await this.requestJson('/api/history/resources/'+encodeURIComponent(this.target.resourceId)+'/changes?'+params, {signal:controller.signal});
      if(!this.guardContext(token)||controller.signal.aborted||this.changeController!==controller)return;
      if(data.resource_id!==this.target.resourceId||data.version!==1||data.coverage!=='recorded_receipts_only'||data.order!=='action_id'||!Number.isSafeInteger(data.revision)||!Array.isArray(data.items)||data.items.length>PAGE_LIMIT||
        (data.next_cursor!=null&&typeof data.next_cursor!=='string')||data.items.some(item=>!item||typeof item.action_id!=='string'||!item.action_id))throw new Error('History returned an invalid Changes page.');
      this.changes={items:data.items,next:data.next_cursor||null,busy:false,error:''};
    } catch(error) {
      if(error?.name!=='AbortError'&&!controller.signal.aborted&&this.changeController===controller&&this.guardContext(token))this.changes={items:[],next:null,busy:false,error:this.errorMessage(error,'list')};
    } finally { if(this.changeController===controller)this.changeController=null; if(this.isCurrent(token))this.render(); }
  }

  async loadManagedChanges(cursor = null) {
    this.changeSets?.clearAction();
    const token=this.generation;if(!this.guardContext(token))return;
    this.changeController?.abort();const controller=new AbortController();this.changeController=controller;
    this.managedChanges={groups:[],next:null,busy:true,error:''};this.render();
    try {
      const params=new URLSearchParams({limit:String(PAGE_LIMIT)});if(cursor)params.set('cursor',cursor);
      const data=await this.requestJson('/api/history/resources/'+encodeURIComponent(this.target.resourceId)+'/managed-change-fragments?'+params,{signal:controller.signal});
      if(!this.guardContext(token)||controller.signal.aborted||this.changeController!==controller)return;
      if(data.resource_id!==this.target.resourceId||data.version!==1||data.scope!=='one_authorized_resource'||data.coverage!=='page_fragment'||data.order!=='action_id'||!Number.isSafeInteger(data.revision)||!Array.isArray(data.groups)||data.groups.length>PAGE_LIMIT||data.groups.some(group=>!group||!Array.isArray(group.actions)||!group.actions.length)||(data.next_cursor!=null&&typeof data.next_cursor!=='string'))throw new Error('Invalid managed change page.');
      this.managedChanges={groups:data.groups,next:data.next_cursor||null,busy:false,error:''};
    } catch(error) {if(error?.name!=='AbortError'&&!controller.signal.aborted&&this.changeController===controller&&this.guardContext(token))this.managedChanges={groups:[],next:null,busy:false,error:this.errorMessage(error,'list')};}
    finally {if(this.changeController===controller)this.changeController=null;if(this.isCurrent(token))this.render();}
  }

  async loadOrderedChanges(selector = null, cursor = null) {
    this.changeSets?.clearAction();
    const token=this.generation;if(!this.guardContext(token))return;
    this.changeController?.abort();const controller=new AbortController();this.changeController=controller;
    this.managedChanges={groups:[],next:null,busy:true,error:'',ordered:true,selector};this.render();
    try {
      const params=new URLSearchParams({limit:String(PAGE_LIMIT)});if(cursor)params.set('cursor',cursor);if(selector)params.set('origin_action_id',selector);
      const data=await this.requestJson('/api/history/resources/'+encodeURIComponent(this.target.resourceId)+'/ordered-managed-changes?'+params,{signal:controller.signal});
      if(!this.guardContext(token)||controller.signal.aborted||this.changeController!==controller)return;
      if(data.resource_id!==this.target.resourceId||data.version!==1||data.scope!=='one_authorized_resource'||data.coverage!==(selector?'recorded_origin_only':'recorded_timestamps_only')||data.order!==(selector?'action_id':'recorded_timestamp_action_id')||!Number.isSafeInteger(data.revision)||!Array.isArray(data.items)||data.items.length>PAGE_LIMIT||(data.next_cursor!=null&&typeof data.next_cursor!=='string'))throw new Error('Invalid ordered change page.');
      this.managedChanges={groups:data.items.map(item=>({group_id:null,actor_id:null,actions:[item]})),next:data.next_cursor||null,busy:false,error:'',ordered:true,selector};
    } catch(error) {if(error?.name!=='AbortError'&&!controller.signal.aborted&&this.changeController===controller&&this.guardContext(token))this.managedChanges={groups:[],next:null,busy:false,error:this.errorMessage(error,'list'),ordered:true,selector};}
    finally {if(this.changeController===controller)this.changeController=null;if(this.isCurrent(token))this.render();}
  }

  async loadOriginFragments(selector, cursor = null, mode = 'origin') {
    this.changeSets?.clearAction();
    const token = this.generation;
    if (!this.guardContext(token)) return;
    this.originController?.abort();
    const controller = new AbortController(); this.originController = controller;
    const prior = this.originFragments?.selector === selector && this.originFragments?.mode === mode ? this.originFragments : null;
    this.originFragments = {mode, selector, items: prior?.items || [], next: prior?.next || null, partial: prior?.partial || false, busy: true, error: ''}; this.render();
    try {
      const params = new URLSearchParams(mode==='origin'?{origin_action_id: selector}:{}); if (cursor) params.set('cursor', cursor);
      const data = await this.requestJson('/api/history/resources/' + encodeURIComponent(this.target.resourceId) + (mode==='root'?'/workspace-recorded-activity?':'/cross-resource-origin?') + params, {signal: controller.signal});
      if (!this.guardContext(token) || controller.signal.aborted || this.originController !== controller) return;
      if (data.version !== 1 || (mode==='root'?(data.scope!=='one_registered_root'||data.coverage!=='partial_recorded_activity'||data.order!=='recorded_timestamp_action_resource'):(data.coverage!=='recorded_origin_fragment'||data.order!=='recorded_time_then_unknown'||typeof data.partial!=='boolean')) || !Array.isArray(data.items) || data.items.length > 20 || (data.next_cursor != null && (typeof data.next_cursor !== 'string'||data.next_cursor.length>16384)) || data.items.some(x => !x || !/^resource-[0-9a-f]{64}$/.test(x.resource_id||'') || typeof x.name !== 'string' || x.name.length>1024 || (x.recorded_millis != null && (typeof x.recorded_millis !== 'string' || !/^[0-9]{1,20}$/.test(x.recorded_millis))) || typeof x.state !== 'string' || (mode==='origin'&&typeof x.availability !== 'string') || (x.locator!=null&&(typeof x.locator!=='string'||x.locator.length>4096)))) throw new Error('Invalid activity page.');
      this.originFragments = {mode, selector, items: data.items, next: data.next_cursor || null, partial: mode==='root'||data.partial, busy: false, error: ''};
    } catch (error) {
      if (error?.name !== 'AbortError' && !controller.signal.aborted && this.originController === controller && this.guardContext(token)) this.originFragments = {mode, selector, items: prior?.items || [], next: prior?.next || null, partial: prior?.partial || mode==='root', busy: false, error: error?.status === 409 ? 'This page is stale. Refresh the recorded activity to continue.' : this.errorMessage(error, 'list')};
    } finally { if (this.originController === controller) { this.originController = null; if (this.originFragments?.busy) this.originFragments = {...this.originFragments, busy:false}; } if (this.isCurrent(token)) this.render(); }
  }

  openCapturedAction(data, {expectedResource=null, name='Captured change', locator=null}={}) {
    const token=this.generation, grouped=data?.version===2;
    const members=grouped ? data.members : [{current_resource_id:data?.current_resource_id,item:data?.item,state:'available'}];
    const selected=expectedResource ? members?.find(x=>x.current_resource_id===expectedResource) : members?.[0];
    const publicID=selected?.current_resource_id;
    if(!this.guardContext(token)||!/^resource-[0-9a-f]{64}$/.test(publicID||'')||!selected)return false;
    if(this.target.resourceId!==publicID){const scopeCurrent=this.target.isContextCurrent,onRecordedImpact=this.target.onRecordedImpact;if(!this.open({resourceId:publicID,name,isContextCurrent:scopeCurrent,onRecordedImpact,exactOnly:true}))return false;}
    this.observedStepController?.abort(); this.observedStepController=null; this.observedStep=null;
    this.capturedActionLocator=isCapturedLocator(locator) ? locator : null;
    this.capturedGroup=grouped ? {actionID:data.action_id,members:data.members,locator:this.capturedActionLocator} : null;
    this.changes={items:grouped?[]:[data.item],next:null,busy:false,error:''};
    this.statusMessage=grouped?'One recorded action; coverage is partial.':'One recorded action only; other changes in this turn are not covered.';
    this.render();if(!grouped)void this.compareChange(data.item);return true;
  }

  async analyzeCapturedImpact(resourceID, item) {
    const token=this.generation, locator=this.capturedActionLocator, callback=this.target?.onRecordedImpact;
    const action=item?.action_id, version=item?.after?.version_id;
    if(this.impactPending || !this.guardContext(token)||!isCapturedLocator(locator)||typeof callback!=='function'
        || !/^resource-[0-9a-f]{64}$/.test(resourceID||'') || typeof action!=='string' || !action
        || typeof version!=='string' || !version || item?.ambiguous_phase) return;
    const valid=()=>this.guardContext(token)&&this.capturedActionLocator===locator
      && (this.capturedGroup ? this.capturedGroup.members.some(member=>member.current_resource_id===resourceID && member.state==='available' && member.item===item)
        : this.target.resourceId===resourceID && this.changes?.items?.includes(item));
    const pending={};this.impactPending=pending;this.render();
    try {
      const result=await callback({locator,resourceID,actionID:action,versionID:version,isCurrent:valid});
      if(valid()){
        if(result?.kind==='recorded-comparison' && ['recorded_matches_current','current_differs_from_recorded'].includes(result.state) && ['current','unknown'].includes(result.index)){
          this.statusMessage=(result.state==='recorded_matches_current'?'Recorded bytes match the current file.':'Current file bytes differ from the recorded version.')+' One file at the time of the check; index '+(result.index==='current'?'verified current':'status unknown')+'. Impact is unavailable.';
          if(result.retrospective){const r=result.retrospective;this.statusMessage+=` Current-parser reconstruction of this one recorded file: ${r.matched} matching declaration keys, ${r.recordedOnly} only in the recorded file and ${r.currentOnly} only in the current file. These are syntactic keys, not semantic removals or renames.`;}
          this.statusBad=false;
        }else{this.statusMessage=result===true?'Recorded-file impact displayed.':'Recorded-file impact unavailable or changed.';this.statusBad=result!==true;}
        this.render();
      }
    } catch {if(valid()){this.statusMessage='Recorded-file impact unavailable or changed.';this.statusBad=true;this.render();}}
    finally {if(this.impactPending===pending){this.impactPending=null;if(this.guardContext(token))this.render();}}
  }

  async compareCapturedMember(resourceID) {
    const group=this.capturedGroup, token=this.generation;
    if(!group?.locator || !this.guardContext(token))return;
    try {
      const data=await resolveCapturedChange(group.locator,()=>this.guardContext(token)&&this.capturedGroup===group);
      const member=data?.version===2 && data.action_id===group.actionID && data.members.find(x=>x.current_resource_id===resourceID);
      if(!this.guardContext(token)||this.capturedGroup!==group||!member||member.state!=='available'||!member.item)throw Error('Member unavailable.');
      await this.compareChange(member.item,resourceID);
    } catch (_) {
      if(this.guardContext(token)&&this.capturedGroup===group){this.statusMessage='Recorded member is unavailable or changed.';this.render();}
    }
  }

  async loadObservedStep({selector = null, cursor = null} = {}) {
    const token = this.generation, locator = this.capturedActionLocator;
    const account = document.body.dataset.accountId;
    const scope = window.chatModule?.ratingAccountScope?.();
    const identity = scope && {owner:scope.owner, sid:scope.sid, authEpoch:scope.authEpoch};
    if (!locator || !account || !this.guardContext(token)) return;
    const current = () => {
      const now = window.chatModule?.ratingAccountScope?.();
      return document.body.dataset.accountId === account && this.guardContext(token)
        && this.capturedActionLocator === locator
        && (!identity || (!!now && now.owner === identity.owner && now.sid === identity.sid && now.authEpoch === identity.authEpoch));
    };
    const prior = this.observedStep;
    if (selector && (!prior?.chatID || !prior.candidates?.some(item => item.selector === selector))) return;
    this.observedStepController?.abort();
    const controller = new AbortController(); this.observedStepController = controller;
    this.observedStep = {...(prior || {}), busy:true, error:''}; this.render();
    try {
      const body = selector ? {version:1,chatID:prior.chatID,selector,...(cursor ? {cursor} : {})} : {version:1,locator};
      const data = await this.requestJson('/api/history/observed-step', {method:'POST', signal:controller.signal,
        headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
      if (!current() || controller.signal.aborted || this.observedStepController !== controller) return;
      if (data.version !== 1 || data.coverage !== 'partial' || data.assurance !== 'unverified_local_observation') throw Error('Invalid observed step response.');
      if (!selector) {
        if (data.kind !== 'step_candidates' || typeof data.chatID !== 'string' || !data.chatID || data.chatID.length > 256
          || !Array.isArray(data.candidates) || data.candidates.length > 20 || data.candidates.some(item => !item || typeof item.selector !== 'string'
            || !item.selector.startsWith('oc-snapshot:step:v1:') || item.selector.length > 4096 || typeof item.coverage !== 'string' || item.coverage.length > 128)) throw Error('Invalid observed step candidates.');
        this.observedStep = {chatID:data.chatID, candidates:data.candidates, members:null, next:null, busy:false, error:''};
      } else {
        if (data.selector !== selector || !Array.isArray(data.members) || data.members.length > 20
          || (data.next_cursor != null && (typeof data.next_cursor !== 'string' || data.next_cursor.length > 16384))
          || data.members.some(item => !item || !Number.isSafeInteger(item.sequence) || item.sequence < 0
            || !['qualified','pending','unavailable'].includes(item.state)
            || (item.state === 'qualified' && (!isCapturedLocator(item.locator) || (item.locator.startsWith('oc-history-action:v2:')
              ? !validObservedGroupMembers(item.members)
              : (!/^resource-[0-9a-f]{64}$/.test(item.current_resource_id || '') || typeof item.current_name !== 'string' || item.current_name.length > 1024))))
            || (item.state !== 'qualified' && ('current_resource_id' in item || 'current_name' in item || 'locator' in item)))) throw Error('Invalid observed step members.');
        this.observedStep = {...prior, selector, members:data.members, next:data.next_cursor || null, busy:false, error:''};
      }
    } catch (error) {
      if (error?.name !== 'AbortError' && !controller.signal.aborted && this.observedStepController === controller && current())
        this.observedStep = {...(prior || {}), busy:false, error:error?.status === 409 ? 'This observation is unavailable or stale. Refresh candidates explicitly.' : 'Observed step unavailable.'};
    } finally {
      if (this.observedStepController === controller) { this.observedStepController = null; if (this.observedStep?.busy) this.observedStep.busy = false; }
      if (this.isCurrent(token)) this.render();
    }
  }

  async compareChange(item, resourceID=this.target?.resourceId) {
    const token=this.generation;
    if(!this.guardContext(token))return;
    this.changeCompareController?.abort();
    const controller=new AbortController(); this.changeCompareController=controller;
    this.changeComparison={action:item.action_id,resourceID,busy:true}; this.render();
    const read=async version=>{
      if(!version?.id)return {state:'unknown'};
      if(version.availability==='absent')return {state:'absence recorded at page load'};
      if(version.availability!=='available'||version.can_read!==true)return {state:String(version.availability||'unavailable')};
      const chunks=[];let offset=0,eof=false;
      while(offset<PREVIEW_LIMIT_BYTES&&!eof){
        const limit=Math.min(READ_CHUNK_BYTES,PREVIEW_LIMIT_BYTES-offset);
        const url='/api/history/resources/'+encodeURIComponent(resourceID)+'/versions/'+encodeURIComponent(version.id)+'?offset='+offset+'&limit='+limit+((this.target.exactOnly||this.capturedGroup)?'&strict_account=true':'');
        const data=await this.requestJson(url,{signal:controller.signal});
        if(!this.guardContext(token)||controller.signal.aborted)throw new DOMException('Cancelled','AbortError');
        if(data.resource_id!==resourceID||data.version?.id!==version.id||data.offset!==offset||data.content_encoding!=='base64'||(data.content!=null&&typeof data.content!=='string'))throw new Error('Invalid version response.');
        const raw=data.content==null?'':atob(data.content);if(raw.length>limit)throw new Error('Oversized version response.');
        const chunk=Uint8Array.from(raw,c=>c.charCodeAt(0));chunks.push(chunk);offset+=chunk.length;eof=data.eof===true;
        if(!chunk.length&&!eof)throw new Error('Incomplete version response.');
      }
      if(!eof)return {state:'over comparison limit'};
      const bytes=new Uint8Array(offset);let at=0;for(const chunk of chunks){bytes.set(chunk,at);at+=chunk.length;}
      if(bytes.includes(0))return {state:'binary'};
      try{return {state:'text',text:new TextDecoder('utf-8',{fatal:true}).decode(bytes)};}catch{return {state:'binary or non-UTF-8'};}
    };
    try{
      const [before,after]=await Promise.all([read(item.before),read(item.after)]);
      if(!this.guardContext(token)||controller.signal.aborted||this.changeCompareController!==controller)return;
      this.changeComparison={action:item.action_id,resourceID,busy:false,before,after};
    }catch(error){if(error?.name!=='AbortError'&&!controller.signal.aborted&&this.changeCompareController===controller&&this.guardContext(token))this.changeComparison={action:item.action_id,resourceID,busy:false,error:'Exact comparison unavailable: '+String(error?.message||'read failed')};}
    finally{if(this.changeCompareController===controller)this.changeCompareController=null;if(this.isCurrent(token))this.render();}
  }

  async compareCheckpoint(requestId, load) {
    const token=this.generation;
    if(!this.guardContext(token) || typeof load!=='function' || typeof requestId!=='string')return;
    this.checkpointController?.abort();const controller=new AbortController();this.checkpointController=controller;
    this.checkpointComparison={busy:true};this.render();
    try {
      const value=await load({signal:controller.signal});
      if(!this.guardContext(token)||controller.signal.aborted||this.checkpointController!==controller)return;
      const validSide=(side)=>{
        if(!side || typeof side!=='object' || Array.isArray(side) || !['text','binary','oversize','unavailable'].includes(side.state))return false;
        const keys=Object.keys(side);if(keys.some(key=>!['state','text','fingerprint'].includes(key)))return false;
        if(side.state==='text' && (typeof side.text!=='string' || new TextEncoder().encode(side.text).length>PREVIEW_LIMIT_BYTES))return false;
        if(['text','binary'].includes(side.state) && !('fingerprint' in side))return false;
        if(side.state!=='text' && 'text' in side)return false;
        return !('fingerprint' in side) || (typeof side.fingerprint==='string' && /^sha256:[0-9a-f]{64}:(0|[1-9][0-9]*)$/.test(side.fingerprint));
      };
      if(!value || typeof value!=='object' || Array.isArray(value) || Object.keys(value).sort().join(',')!=='checkpoint,coverage,current,request_id,resource_id,version' || value.version!==1 || value.coverage!=='one_selected_checkpoint_file' || value.resource_id!==this.target.resourceId || value.request_id!==requestId || !validSide(value.checkpoint) || !validSide(value.current))throw new Error('Comparison unavailable.');
      this.checkpointComparison={busy:false,checkpoint:value.checkpoint,current:value.current};
    } catch(error) {
      if(error?.name!=='AbortError' && !controller.signal.aborted && this.guardContext(token))this.checkpointComparison={busy:false,error:'Checkpoint comparison unavailable.'};
    } finally {if(this.checkpointController===controller)this.checkpointController=null;if(this.isCurrent(token))this.render();}
  }

  async previewCheckpointRestore(checkpointID, client) {
    const token=this.generation, resourceID=this.target?.resourceId, account=document.body.dataset.accountId;
    if(!account || !this.guardContext(token) || this.checkpointPendingForAccount() || this.regularRestorePending || !checkpointID || !resourceID)return;
    const controller=new AbortController();this.checkpointController?.abort();this.checkpointController=controller;
    this.checkpointRestore={busy:true};this.render();
    try {
      const value=await client.previewCheckpointRestore({version:1,checkpoint_id:checkpointID,resource_id:resourceID},{signal:controller.signal});
      if(!this.guardContext(token)||controller.signal.aborted||this.checkpointController!==controller||document.body.dataset.accountId!==account)return;
      const r=value?.review, sourceFingerprint=x=>typeof x==='string'&&/^sha256:[a-f0-9]{64}:(0|[1-9][0-9]*)$/.test(x), destinationFingerprint=x=>typeof x==='string'&&/^[a-f0-9]{64}$/.test(x);
      if(value?.version!==1||value.state!=='ready'||value.checkpoint_id!==checkpointID||value.resource_id!==resourceID||typeof value.request_id!=='string'||!value.request_id||typeof value.expected_digest!=='string'||!value.expected_digest||!r||typeof r.name!=='string'||!sourceFingerprint(r.source_fingerprint)||!destinationFingerprint(r.destination_fingerprint)||!Number.isSafeInteger(r.source_size)||r.source_size<0||!Number.isSafeInteger(r.destination_size)||r.destination_size<0||r.effect!=='replace'||r.captures_current_destination!==true||r.consistency!=='one_file')throw Error('Invalid restore review');
      this.checkpointRestore={client,account,checkpointID,resourceID,requestID:value.request_id,digest:value.expected_digest,review:r,isStatusCurrent:this.target.isStatusCurrent};
    } catch(error) {if(!controller.signal.aborted&&this.guardContext(token))this.checkpointRestore={error:'Checkpoint restore preview is unavailable.'};}
    finally {if(this.checkpointController===controller)this.checkpointController=null;if(this.isCurrent(token))this.render();}
  }

  async issueCheckpointRestore(statusOnly=false) {
    const token=this.generation, entry=statusOnly?this.checkpointRestorePending:this.checkpointRestore;
    const statusCurrent=()=>this.isCurrent(token)&&document.body.dataset.accountId===entry?.account&&typeof entry?.isStatusCurrent==='function'&&entry.isStatusCurrent();
    if(!entry || !(statusOnly?statusCurrent():this.guardContext(token)) || document.body.dataset.accountId!==entry.account || this.restoring || (!statusOnly && (this.checkpointPendingForAccount() || this.regularRestorePending)))return;
    if(!statusOnly)this.checkpointRestorePending={client:entry.client,account:entry.account,requestID:entry.requestID,digest:entry.digest,resourceID:entry.resourceID,isStatusCurrent:entry.isStatusCurrent};
    this.restoring=true;this.render();
    try {
      const selector={version:1,request_id:entry.requestID,expected_digest:entry.digest};
      const result=statusOnly?await entry.client.checkpointRestoreStatus(selector):await entry.client.confirmCheckpointRestore({...selector,checkpoint_id:entry.checkpointID,resource_id:entry.resourceID});
      if(!statusCurrent())return;
      if(result?.version!==1||!['pending','complete','conflict','partial','unknown','refused','status_only'].includes(result.state)||result.request_id!==entry.requestID||result.expected_digest!==entry.digest)throw Error('Invalid receipt');
      if(result.state==='complete' && result.resource_id!==entry.resourceID)throw Error('Invalid completed resource');
      if(this.target?.resourceId===entry.resourceID)this.checkpointRestore={...entry,result};
      if(['complete','conflict','refused','partial'].includes(result.state))this.checkpointRestorePending=null;
      if(result.state==='complete' && result.resource_id===entry.resourceID) {
        if(this.contextMatches() && this.target?.resourceId===entry.resourceID && this.target?.onRestored) {
          try { await this.target.onRestored({resourceId:entry.resourceID,receipt:result}); }
          catch { this.statusMessage='Restore completed; the Files view could not be refreshed.';this.statusBad=true; }
        }
      }
    } catch {if(statusCurrent()&&this.target?.resourceId===entry.resourceID)this.checkpointRestore={...entry,result:{state:'unknown'}};}
    finally {this.restoring=false;if(this.isCurrent(token))this.render();}
  }

  async reviewRegularRestore(source) {
    const token=this.generation, target=this.target, account=document.body.dataset.accountId;
    const scope=window.chatModule?.ratingAccountScope?.();
    const auth=scope && {owner:scope.owner,authEpoch:scope.authEpoch};
    const selected=this.selectedRef;
    if (!account || !auth || auth.owner!==account || auth.authEpoch==null || !target || !this.guardContext(token) || this.restoring || this.regularRestorePending || this.checkpointPendingForAccount()
      || !source || (typeof source.version_ref !== 'string' && typeof source.inverse_request_id !== 'string')) return;
    if(!this.regularRestoreAccounts.has(account) && this.regularRestoreAccounts.size>=16){
      for(const [key,value] of this.regularRestoreAccounts) {if(!value.pending){this.regularRestoreAccounts.delete(key);break;}}
      if(this.regularRestoreAccounts.size>=16){this.regularRestore={error:'Too many uncertain restore receipts. Resolve an existing original status before starting another.'};this.render();return;}
    }
    const current=()=>document.body.dataset.accountId===account && this.guardContext(token) && this.target===target
      && (!source.version_ref || this.selectedRef===selected) && (!auth || (()=>{const now=window.chatModule?.ratingAccountScope?.();return !!now&&now.owner===auth.owner&&now.authEpoch===auth.authEpoch;})());
    const controller=new AbortController(); this.regularRestoreController?.abort();this.regularRestoreController=controller;
    this.regularRestore={busy:true};this.render();
    try {
      const value=await this.requestJson('/api/history/restore',{method:'POST',signal:controller.signal,headers:{'Content-Type':'application/json'},body:JSON.stringify({profile:'regular_file_v1',phase:'preview',resource_id:target.resourceId,source})});
      if(!current() || controller.signal.aborted || this.regularRestoreController!==controller)return;
      const state=x=>x && (x.state==='Absent' ? Object.keys(x).length===1 : x.state==='Present' && /^[a-f0-9]{64}$/.test(x.sha256||'') && Number.isSafeInteger(x.size) && x.size>=0 && Number.isInteger(x.mode) && x.mode>=0 && x.mode<=511);
      const r=value?.review;
      if(value?.version!==1 || value.profile!=='regular_file_v1' || value.state!=='ready' || value.resource_id!==target.resourceId
        || JSON.stringify(value.source)!==JSON.stringify(source) || typeof value.request_id!=='string' || !value.request_id || typeof value.expected_digest!=='string' || !/^[a-f0-9]{64}$/.test(value.expected_digest)
        || !r || typeof r.name!=='string' || r.name.length>1024 || !state(r.current) || !state(r.desired)
        || !['create','replace','delete'].includes(r.effect) || r.consistency!=='optimistic_path_guard') throw Error('Invalid regular file review');
      this.regularRestore={account,auth,resourceID:target.resourceId,source,requestID:value.request_id,digest:value.expected_digest,review:r,isStatusCurrent:target.isStatusCurrent,selected};
    } catch(error) {if(!controller.signal.aborted && current())this.regularRestore={error:'Regular-file review unavailable.'};}
    finally {if(this.regularRestoreController===controller)this.regularRestoreController=null;if(this.isCurrent(token))this.render();}
  }

  async issueRegularRestore(statusOnly=false) {
    const token=this.generation, entry=statusOnly?this.regularRestorePending:this.regularRestore;
    const currentScope=window.chatModule?.ratingAccountScope?.();
    const requestAuth=currentScope && {owner:currentScope.owner,authEpoch:currentScope.authEpoch};
    const statusCurrent=()=>{
      try {
        if(!entry || !requestAuth || requestAuth.owner!==entry.account || requestAuth.authEpoch==null || document.body.dataset.accountId!==entry.account)return false;
        const now=window.chatModule?.ratingAccountScope?.();
        return !!now&&now.owner===requestAuth.owner&&now.authEpoch===requestAuth.authEpoch;
      } catch {return false;}
    };
    if(!entry || !statusCurrent() || this.restoring || (!statusOnly && (!entry.isStatusCurrent?.() || (entry.auth && (!requestAuth || entry.auth.owner!==requestAuth.owner || entry.auth.authEpoch!==requestAuth.authEpoch)) || !this.guardContext(token) || this.target?.resourceId!==entry.resourceID
      || (entry.source.version_ref && this.selectedRef!==entry.selected) || this.regularRestorePending || this.checkpointPendingForAccount())))return;
    if(!statusOnly)this.regularRestorePending={...entry};
    this.restoring=true;this.render();
    try {
      const body={profile:'regular_file_v1',phase:statusOnly?'status':'confirm',resource_id:entry.resourceID,source:entry.source,request_id:entry.requestID,expected_digest:entry.digest};
      const result=await this.requestJson('/api/history/restore',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      if(!statusCurrent())return;
      if(result?.version!==1 || !['pending','unknown','status_only','complete','conflict','refused','partial'].includes(result.state)
        || result.request_id!==entry.requestID || result.expected_digest!==entry.digest || (result.resource_id!=null && result.resource_id!==entry.resourceID)
        || (result.verified!=null && typeof result.verified!=='boolean') || (result.current_matches!=null && typeof result.current_matches!=='boolean')
        || (result.state==='complete' && (result.resource_id!==entry.resourceID || result.verified!==true)))throw Error('Invalid regular restore receipt');
      this.regularRestoreResult={entry,result};
      if(['complete','conflict','refused','partial'].includes(result.state))this.regularRestorePending=null;
      if(this.target?.resourceId===entry.resourceID)this.regularRestore=null;
      if(result.state==='complete' && this.contextMatches() && this.target?.resourceId===entry.resourceID && this.target.onRestored) {
        try {const refresh=await this.target.onRestored({resourceId:entry.resourceID,receipt:result});
          if(statusCurrent() && refresh?.status && refresh.status!=='refreshed'){this.statusMessage='Restore completed; the Files view could not be refreshed.';this.statusBad=true;}}
        catch {if(statusCurrent()){this.statusMessage='Restore completed; the Files view could not be refreshed.';this.statusBad=true;}}
      }
    } catch {if(statusCurrent())this.regularRestoreResult={entry,result:{state:'unknown'}};}
    finally {this.restoring=false;this.render();}
  }

  async loadPage(cursor = null) {
    const token = this.generation;
    if (!this.guardContext(token)) return;
    this.listController?.abort();
    const controller = new AbortController();
    this.listController = controller;
    this.listBusy = true;
    this.listError = '';
    this.render();
    try {
      const response = await this.requestJson(this.versionsUrl(cursor), { signal: controller.signal });
      if (!this.guardContext(token) || controller.signal.aborted) return;
      if (response.resource_id !== this.target.resourceId || !Array.isArray(response.items)
        || response.items.length > PAGE_LIMIT
        || (response.next_cursor != null && typeof response.next_cursor !== 'string')
        || response.items.some((item) => !item || typeof item !== 'object' || typeof item.id !== 'string' || !item.id)) {
        throw new Error('History returned an invalid resource-version page.');
      }
      this.items = response.items;
      this.nextCursor = response.next_cursor || null;
      if (this.selectedRef) {
        const updated = this.items.find((item) => item.id === this.selectedRef);
        if (updated) this.selectedVersion = updated;
      }
      this.listError = '';
    } catch (error) {
      if (error?.name !== 'AbortError' && this.guardContext(token)) {
        this.listError = this.errorMessage(error, 'list');
      }
    } finally {
      if (this.listController === controller) this.listController = null;
      if (this.isCurrent(token)) {
        this.listBusy = false;
        this.render();
      }
    }
  }

  errorMessage(error, operation) {
    const status = Number(error?.status || 0);
    const detail = String(error?.message || '');
    if (operation === 'restore' && status >= 500) return 'History service did not return a restore receipt. The outcome is unknown; inspect the resource before retrying.';
    if (status === 503 || status === 502 || status === 504) return 'History service is unavailable. Retry when the service is available.';
    if (status === 401 || status === 403) return 'This Files resource is no longer authorized for History.';
    if (status === 410) return 'Retention has pruned this version. Its content can no longer be read or restored.';
    if (status === 404) return 'This resource or version is no longer registered or available to History.';
    if (status === 409 && operation === 'restore') return 'The destination changed after review. No restore was applied; review the current destination again.';
    if (status === 409) return 'This version is changing or unavailable. Refresh History and review it again.';
    if (!status && operation === 'restore') return 'History did not return a restore receipt. The restore outcome is unknown; review the resource before retrying.';
    return detail || 'History could not complete this request.';
  }

  movePage(direction) {
    if (this.listBusy || this.restoring || this.contextInvalid) return;
    if (direction < 0 && this.pageIndex > 0) {
      this.pageIndex -= 1;
      void this.loadPage(this.pageCursors[this.pageIndex]);
      return;
    }
    if (direction > 0 && this.nextCursor) {
      this.pageCursors = this.pageCursors.slice(0, this.pageIndex + 1);
      this.pageCursors.push(this.nextCursor);
      this.pageIndex += 1;
      void this.loadPage(this.nextCursor);
    }
  }

  selectVersion(version, index) {
    if (this.restoring || this.contextInvalid || !version?.id) return;
    this.previewController?.abort();
    this.previewController = null;
    this.restorePreviewController?.abort();
    this.restorePreviewController = null;
    this.previewGeneration += 1;
    this.selectedRef = version.id;
    this.selectedVersion = version;
    this.previewBusy = false;
    this.previewError = '';
    this.previewText = '';
    this.previewNote = '';
    this.restorePreview = null;
    this.regularRestoreController?.abort(); this.regularRestoreController=null; this.regularRestore=null;
    this.restorePreviewBusy = false;
    this.render();
    void this.loadPreview(this.generation, version.id, version);
  }

  async loadPreview(token, versionRef, version) {
    if (!this.guardContext(token) || !this.isCurrent(token, versionRef)) return;
    if (version.can_read !== true || version.availability !== 'available') {
      this.previewNote = this.availabilityNote(version);
      this.render();
      return;
    }
    const previewToken = ++this.previewGeneration;
    const controller = new AbortController();
    this.previewController = controller;
    this.previewBusy = true;
    this.previewError = '';
    this.previewNote = '';
    this.previewText = '';
    this.render();
    try {
      const bytes = [];
      let offset = 0;
      let eof = false;
      while (offset < PREVIEW_LIMIT_BYTES && !eof) {
        if (!this.guardContext(token) || !this.isCurrent(token, versionRef) || previewToken !== this.previewGeneration) return;
        const limit = Math.min(READ_CHUNK_BYTES, PREVIEW_LIMIT_BYTES - offset);
        const url = '/api/history/resources/' + encodeURIComponent(this.target.resourceId)
          + '/versions/' + encodeURIComponent(versionRef)
          + '?offset=' + String(offset) + '&limit=' + String(limit);
        const response = await this.requestJson(url, { signal: controller.signal });
        if (!this.guardContext(token) || controller.signal.aborted || !this.isCurrent(token, versionRef)
          || previewToken !== this.previewGeneration) return;
        if (response.resource_id !== this.target.resourceId || response.version?.id !== versionRef
          || response.offset !== offset || response.content_encoding !== 'base64'
          || (response.content != null && typeof response.content !== 'string')) {
          throw new Error('History returned an invalid version preview.');
        }
        if (response.content == null) {
          if (!response.eof) throw new Error('History returned an incomplete version preview.');
          eof = true;
          break;
        }
        const raw = atob(response.content);
        const chunk = new Uint8Array(raw.length);
        for (let index = 0; index < raw.length; index += 1) chunk[index] = raw.charCodeAt(index);
        if (chunk.length > limit) throw new Error('History returned an oversized version preview.');
        bytes.push(chunk);
        offset += chunk.length;
        eof = response.eof === true;
        if (!chunk.length && !eof) throw new Error('History returned an incomplete version preview.');
      }
      const combined = new Uint8Array(offset);
      let cursor = 0;
      for (const chunk of bytes) { combined.set(chunk, cursor); cursor += chunk.length; }
      if (combined.includes(0)) {
        this.previewNote = 'This version is binary; text preview is unavailable.';
      } else {
        try {
          this.previewText = new TextDecoder('utf-8', { fatal: true }).decode(combined);
          if (!this.previewText && eof) this.previewNote = 'This version contains an empty file.';
        } catch (_) {
          this.previewNote = 'This version is not readable as UTF-8 text.';
        }
      }
      if (!eof && offset >= PREVIEW_LIMIT_BYTES) this.previewNote = 'Preview is limited to the first 256 KiB.';
    } catch (error) {
      if (error?.name !== 'AbortError' && this.guardContext(token) && this.isCurrent(token, versionRef)
        && previewToken === this.previewGeneration) {
        this.previewError = this.errorMessage(error, 'read');
      }
    } finally {
      if (this.previewController === controller) this.previewController = null;
      if (this.isCurrent(token, versionRef) && previewToken === this.previewGeneration) {
        this.previewBusy = false;
        this.render();
      }
    }
  }

  availabilityNote(version) {
    if (version?.availability === 'absent') return 'This entry records an absence. It has no payload to preview or restore.';
    if (version?.availability === 'expired') return 'Retention has pruned this version. Its content can no longer be read or restored.';
    if (version?.availability === 'expiring') return 'This version is being removed by retention and is temporarily unavailable.';
    return 'This version is unavailable for preview. It cannot be restored unless the service marks it available.';
  }

  async reviewRestore() {
    const token = this.generation;
    const versionRef = this.selectedRef;
    const version = this.selectedVersion;
    if (!this.guardContext(token) || !this.isCurrent(token, versionRef) || this.restoring
      || this.regularRestorePending || this.checkpointPendingForAccount() || version?.can_restore !== true || !versionRef) return;
    this.restorePreview = null;
    this.restorePreviewBusy = true;
    this.statusMessage = '';
    this.statusBad = false;
    this.render();
    const controller = this.createRestorePreviewController(token);
    try {
      const url = '/api/history/resources/' + encodeURIComponent(this.target.resourceId)
        + '/versions/' + encodeURIComponent(versionRef) + '/restore-preview';
      const response = await this.requestJson(url, { signal: controller.signal });
      if (!this.guardContext(token) || !this.isCurrent(token, versionRef)) return;
      const preview = response;
      const expected = preview?.destination?.expected_fingerprint;
      const exists = preview?.destination?.exists;
      if (String(preview.resource?.resource_id || '') !== this.target.resourceId
        || preview.source?.id !== versionRef
        || (preview.effect !== 'create' && preview.effect !== 'replace')
        || preview.requires_confirmation !== true
        || preview.captures_current_destination !== true
        || typeof expected !== 'string' || !expected
        || typeof exists !== 'boolean'
        || (!exists && expected !== 'missing')
        || (exists && expected === 'missing')) {
        throw new Error('History returned an incomplete restore preview. Nothing was restored.');
      }
      this.restorePreview = {
        resourceId: this.target.resourceId,
        versionRef,
        destination: preview.destination,
        effect: preview.effect,
      };
    } catch (error) {
      if (error?.name !== 'AbortError' && this.guardContext(token) && this.isCurrent(token, versionRef)) {
        this.statusMessage = this.errorMessage(error, 'preview');
        this.statusBad = true;
      }
    } finally {
      const ownsPreview = this.restorePreviewController === controller;
      if (ownsPreview) this.restorePreviewController = null;
      if (ownsPreview && this.isCurrent(token, versionRef)) {
        this.restorePreviewBusy = false;
        this.render();
      }
    }
  }

  createRestorePreviewController(token) {
    this.restorePreviewController?.abort();
    const controller = new AbortController();
    this.restorePreviewController = controller;
    if (!this.isCurrent(token)) controller.abort();
    return controller;
  }

  async confirmRestore() {
    const token = this.generation;
    const versionRef = this.selectedRef;
    const version = this.selectedVersion;
    const target = this.target;
    const review = this.restorePreview;
    const expected = review?.destination?.expected_fingerprint;
    if (!this.guardContext(token) || !this.isCurrent(token, versionRef) || this.restoring
      || this.regularRestorePending || this.checkpointPendingForAccount() || version?.can_restore !== true || !review || review.resourceId !== target?.resourceId
      || review.versionRef !== versionRef || typeof expected !== 'string' || !expected) return;
    const restoreId = globalThis.crypto?.randomUUID?.()
      || 'history-' + Date.now() + '-' + Math.random().toString(16).slice(2);
    const body = {
      restore_id: restoreId,
      resource_id: target.resourceId,
      version_ref: versionRef,
      expected_destination_fingerprint: expected,
    };
    this.restoring = true;
    this.statusMessage = 'Restoring the reviewed version…';
    this.statusBad = false;
    this.render();
    const controller = new AbortController();
    this.restoreController = controller;
    try {
      const response = await this.requestJson('/api/history/restore', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (!this.isCurrent(token, versionRef)) return;
      const receipt = response?.Restore;
      const restoredResource = Array.isArray(receipt?.resources)
        && receipt.resources.some((item) => item?.resource_id === target.resourceId && item?.outcome === 'Complete');
      if (receipt?.outcome !== 'Complete' || !restoredResource) {
        if (receipt?.outcome === 'Conflict') {
          this.restorePreview = null;
          this.statusMessage = this.errorMessage({ status: 409 }, 'restore');
          this.statusBad = true;
          return;
        }
        if (receipt && typeof receipt.outcome === 'string' && receipt.outcome !== 'Complete') {
          this.restorePreview = null;
          this.statusMessage = 'History reported restore outcome ' + receipt.outcome + '. It did not report a complete restore; inspect the resource before retrying.';
          this.statusBad = true;
          return;
        }
        throw new Error('History did not return a complete restore receipt. The restore outcome is unknown; review the resource before retrying.');
      }
      this.pendingRestoreVerification = receipt.verification?.status === 'Pending' ? body : null;
      this.restorePreview = null;
      if (!this.contextMatches()) {
        this.statusMessage = 'Restored ' + target.name + '. The source context changed, so its open view was left untouched.';
        this.statusBad = false;
        return;
      }
      this.statusMessage = 'Restored ' + target.name + '. Refreshing the open view…';
      this.statusBad = false;
      this.render();
      let refresh = null;
      if (target.onRestored) {
        try {
          refresh = await target.onRestored({ resourceId: target.resourceId, versionRef, receipt });
        } catch (error) {
          refresh = { status: 'refresh_failed', message: String(error?.message || error || '') };
        }
      }
      if (!this.isCurrent(token, versionRef)) return;
      if (refresh?.status === 'refreshed') {
        this.statusMessage = 'Restored ' + target.name + ' and refreshed its open view.';
      } else if (refresh?.status === 'draft_preserved') {
        this.statusMessage = 'Restored ' + target.name + '. An unsaved Editor draft was preserved; reopen the file to view the restored bytes.';
      } else if (refresh?.status === 'context_changed' || !this.contextMatches()) {
        this.statusMessage = 'Restored ' + target.name + '. The source context changed, so its open view was left untouched.';
      } else if (refresh?.status === 'refresh_failed') {
        this.statusMessage = 'Restored ' + target.name + ', but the open view could not be refreshed'
          + (refresh.message ? ': ' + refresh.message : '. Reopen the resource to see its current bytes.');
        this.statusBad = true;
      } else if (!target.onRestored) {
        this.statusMessage = 'Restored ' + target.name + '.';
      } else {
        this.statusMessage = 'Restored ' + target.name + '. Reopen the resource to see its current bytes.';
        this.statusBad = true;
      }
      const proof = receipt.verification;
      if (proof?.status === 'Verified') this.statusMessage += ' Source and restored content hashes verified.';
      else if (proof) this.statusMessage += ' Restore committed; content verification ' + (proof.status === 'Mismatch' ? 'found a mismatch.' : 'is pending.');
    } catch (error) {
      if (error?.name !== 'AbortError' && this.isCurrent(token, versionRef)) {
        if (Number(error?.status) === 409) {
          this.restorePreview = null;
          this.statusMessage = this.errorMessage(error, 'restore');
        } else {
          this.restorePreview = null;
          this.statusMessage = this.errorMessage(error, 'restore');
        }
        this.statusBad = true;
      }
    } finally {
      if (this.restoreController === controller) this.restoreController = null;
      this.restoring = false;
      if (this.isCurrent(token)) this.render();
    }
  }

  render() {
    if (!this.window?.body) return;
    const body = this.window.body;
    const focused = body.contains(document.activeElement)
      ? document.activeElement.getAttribute('data-history-focus') || ''
      : '';
    this.window.setStatus(this.statusMessage, this.statusBad);
    const shell = element('div', { class: 'history-browser' });
    if (this.folder && !this.folder.isContextCurrent() && !this.restoring) {
      shell.append(element('p', { class: 'history-state', text: 'Reopen History from the current folder.' }));
      body.replaceChildren(shell);
      return;
    }
    if (!this.target && this.folder) {
      const folder = this.folder;
      shell.append(actionButton(folder.removedOnly ? 'Folder changes' : 'Removed files',
        () => { if (this.folder === folder) this.openFolder(folder, null, !folder.removedOnly); }, { focusKey: 'folder-mode' }));
      if (folder.busy) shell.append(element('p', { class: 'history-state', text: 'Loading…' }));
      if (folder.error) {
        shell.append(element('p', { class: 'history-state error', text: folder.error }),
          actionButton('Retry', () => { void this.loadFolderPage(folder.cursor); }, { focusKey: 'folder-retry' }));
      }
      if (!folder.busy && !folder.error && !folder.items.length) {
        shell.append(element('p', { class: 'history-state', text: folder.removedOnly
          ? 'No removed files recorded for this folder.' : 'No changes recorded for files directly in this folder.' }));
      }
      folder.items.forEach((item, index) => {
        const label = folderChangeLabel(folder.removedOnly ? { ...item, state: 'removed', at_ms: item.removed_at_ms } : item);
        const button = actionButton('Open versions', () => this.openFolderVersion(folder, item),
          { disabled: folder.busy, focusKey: 'folder-version-' + index });
        button.setAttribute('aria-label', 'Open versions of ' + item.name);
        shell.append(element('div', { class: 'history-browser-context' }, element('span', { text: label }), button));
      });
      if (folder.next != null && !folder.error) {
        shell.append(actionButton('More changes', () => { void this.loadFolderPage(folder.next); },
          { disabled: folder.busy, focusKey: 'folder-more' }));
      }
      body.replaceChildren(shell);
      if (focused) [...body.querySelectorAll('[data-history-focus]')]
        .find(node => node.getAttribute('data-history-focus') === focused)?.focus({ preventScroll: true });
      return;
    }
    if (!this.target) {
      shell.append(element('p', { class: 'history-state', text: 'Open History from a supported Files resource to inspect its captured versions.' }));
      body.replaceChildren(shell);
      return;
    }
    if (this.folder) {
      const folder = this.folder;
      shell.append(actionButton(folder.removedOnly ? 'Back to removed files' : 'Back to folder changes',
        () => this.openFolder(folder, null, folder.removedOnly),
        { disabled: this.restoring || !folder.isContextCurrent(), focusKey: 'folder-back' }));
    }
    shell.append(
      element('div', { class: 'history-browser-context' },
        element('strong', { text: this.target.name }),
        element('span', { text: 'History is scoped to this exact Files resource.' })),
    );
    if (this.statusMessage) shell.append(element('p', {
      class: 'history-status' + (this.statusBad ? ' error' : ''),
      role: 'status',
      'aria-live': 'polite',
      text: this.statusMessage,
    }));
    if(this.checkpointComparison){
      const comparison=this.checkpointComparison, panel=element('section',{class:'history-change-groups','aria-label':'Checkpoint comparison'},
        element('h3',{text:'Checkpoint and current file'}),element('p',{text:'One selected file; current contents were observed separately.'}));
      if(comparison.busy)panel.append(element('p',{text:'Reading comparison…'}));
      else if(comparison.error)panel.append(element('p',{role:'status',text:comparison.error}));
      else for(const [key,label] of [['checkpoint','Checkpoint'],['current','Current']]){
        const side=comparison[key];panel.append(element('strong',{text:label+': '+side.state}));
        if(side.state==='text')panel.append(element('pre',{class:'history-preview-text',text:side.text || '(empty file)'}));
        if(side.fingerprint)panel.append(element('p',{text:'Fingerprint: '+side.fingerprint}));
      }
      shell.append(panel);
    }
    const changeSetSection=this.changeSets?.renderSection();if(changeSetSection)shell.append(changeSetSection);
    if((this.checkpointRestore && (!this.checkpointRestore.account || document.body.dataset.accountId===this.checkpointRestore.account)) || (this.checkpointRestorePending?.isStatusCurrent?.() && document.body.dataset.accountId===this.checkpointRestorePending.account)){
      const item=this.checkpointRestore, pending=this.checkpointRestorePending;
      const panel=element('section',{class:'history-restore-review','aria-label':'Checkpoint restore'},element('h3',{text:'Restore captured file'}));
      if(item?.busy)panel.append(element('p',{text:'Preparing restore review…'}));
      else if(item?.error)panel.append(element('p',{role:'status',text:item.error}));
      else if(item?.review && !pending && this.contextMatches()){
        const r=item.review;panel.append(element('p',{text:`${r.name} · one file · replace current contents. The current destination is captured before replacement.`}),element('p',{text:`Checkpoint: ${r.source_size} bytes · ${r.source_fingerprint}`}),element('p',{text:`Current destination: ${r.destination_size} bytes · ${r.destination_fingerprint}`}));
        panel.append(actionButton('Restore reviewed file',()=>{void this.issueCheckpointRestore(false);},{disabled:this.restoring||this.contextInvalid}),actionButton('Cancel review',()=>{this.checkpointRestore=null;this.render();},{disabled:this.restoring}));
      }
      if(item?.result)panel.append(element('p',{role:'status',text:`Original restore: ${item.result.state}. ${item.result.verified===true?'The original restore was verified; current bytes may have changed.':''}`}));
      if(pending && document.body.dataset.accountId===pending.account && pending.isStatusCurrent?.())panel.append(element('p',{text:'Original restore resource: '+pending.resourceID}),actionButton('Check restore status',()=>{void this.issueCheckpointRestore(true);},{disabled:this.restoring}));
      shell.append(panel);
    }
    const regularPending=this.regularRestorePending, regularResult=this.regularRestoreResult;
    const regularVisible=entry=>Boolean(entry && document.body.dataset.accountId===entry.account);
    if((this.regularRestore && regularVisible(this.regularRestore)) || regularVisible(regularPending) || regularVisible(regularResult?.entry)) {
      const panel=element('section',{'aria-label':'Regular-file restore'},element('h3',{text:'Regular-file state restore'}));
      const entry=this.regularRestore;
      if(entry?.review && regularVisible(entry) && !regularPending) {
        const state=x=>x.state==='Absent'?'Absent':`Present · ${x.size} bytes · SHA-256 ${x.sha256} · mode ${x.mode.toString(8).padStart(4,'0')}`;
        panel.append(element('p',{text:`${entry.review.name} · one file · ${entry.review.effect} · optimistic path guard`}),element('p',{text:'Current: '+state(entry.review.current)}),element('p',{text:'Desired: '+state(entry.review.desired)}),
          actionButton('Confirm regular-file restore',()=>{void this.issueRegularRestore();},{disabled:this.restoring||this.contextInvalid||this.checkpointPendingForAccount()}),
          actionButton('Cancel regular-file review',()=>{this.regularRestore=null;this.render();},{disabled:this.restoring}));
      }
      if(regularVisible(regularResult?.entry)) {
        const result=regularResult.result;panel.append(element('p',{role:'status',text:`Original restore: ${result.state}.${result.state==='complete'?' Verified at completion; current bytes may have changed.':''}`}));
        if(result.state==='complete' && result.verified===true && this.target?.resourceId===regularResult.entry.resourceID && this.contextMatches() && !regularPending)
          panel.append(actionButton('Review inverse',()=>{void this.reviewRegularRestore({inverse_request_id:regularResult.entry.requestID});},{disabled:this.restoring||this.checkpointPendingForAccount()}));
      }
      if(regularVisible(regularPending))panel.append(element('p',{text:'Original restore resource: '+regularPending.resourceID}),actionButton('Check regular restore status',()=>{void this.issueRegularRestore(true);},{disabled:this.restoring}));
      shell.append(panel);
    } else if(this.regularRestore?.error) shell.append(element('p',{class:'history-state error',text:this.regularRestore.error}));
    const toolbar = element('div', { class: 'history-browser-toolbar' },
      actionButton('Refresh versions', () => { void this.loadPage(this.pageCursors[this.pageIndex] || null); }, {
        disabled: this.listBusy || this.restoring || this.contextInvalid,
        focusKey: 'refresh',
      }),
      actionButton('Previous', () => this.movePage(-1), {
        disabled: this.pageIndex <= 0 || this.listBusy || this.restoring || this.contextInvalid,
        focusKey: 'previous',
      }),
      actionButton('Next', () => this.movePage(1), {
        disabled: !this.nextCursor || this.listBusy || this.restoring || this.contextInvalid,
        focusKey: 'next',
      }),
      element('span', { class: 'history-page-label', text: 'Page ' + String(this.pageIndex + 1) }));
    shell.append(toolbar);
    const changes = element('section', { class:'history-change-groups', 'aria-label':'Recorded changes' },
      element('h3',{text:'Recorded changes'}),
      element('p',{text:'One group is one recorded action for this resource, ordered by action ID. Availability is observed at page load; version reads recheck it. Turn-wide coverage is not implied.'}),
      actionButton(this.changes?.busy?'Loading changes…':'Refresh changes',()=>{void this.loadChanges();},{disabled:this.changes?.busy||this.contextInvalid}));
    if(this.changes?.error)changes.append(element('p',{class:'history-state error',text:this.changes.error}));
    if(this.changes&&!this.changes.busy&&!this.changes.error&&!this.changes.items.length)changes.append(element('p',{text:'No recorded actions on this page.'}));
    for(const item of this.changes?.items||[]){
      const row=element('div',{class:'history-version-row'},element('strong',{text:String(item.operation||'Action')+' · '+String(item.state||'unknown')}),
        element('span',{text:(item.writer_identity?'Authenticated principal ':'Actor ')+String(item.actor_id||'unknown')+' · action '+item.action_id}),
        ...(item.writer_identity ? [element('span',{text:'Native writer '+String(item.writer_identity.actor_id)+' · session '+String(item.writer_identity.session_id)+' · source '+String(item.writer_identity.source.actor_id)})] : []),
        element('span',{text:'Coverage '+String(item.coverage||'unknown')+' · binary and rename status unknown'}));
      for(const phase of ['before','after']){
        const version=item[phase];
        if(version&&typeof version.id==='string'&&version.id)row.append(element('span',{text:phase+': '+String(version.availability||'unknown')}));
        else row.append(element('span',{text:phase+': unknown'+(item.ambiguous_phase?' (multiple captures)':'')}));
      }
      row.append(actionButton('Compare exact versions',()=>{void this.compareChange(item);},{disabled:this.contextInvalid||this.changeComparison?.busy}));
      if(this.capturedActionLocator && this.target?.onRecordedImpact && item.after?.id && item.after?.version_id && !item.ambiguous_phase)row.append(actionButton('Analyze current impact',()=>{void this.analyzeCapturedImpact(this.target.resourceId,item);},{disabled:this.contextInvalid||Boolean(this.impactPending)}));
      if(this.changeComparison?.action===item.action_id){
        const comparison=this.changeComparison;
        if(comparison.busy)row.append(element('span',{text:'Reading versions…'}));
        else if(comparison.error)row.append(element('span',{text:comparison.error}));
        else for(const phase of ['before','after']){
          const side=comparison[phase];row.append(element('strong',{text:phase+': '+side.state}));
          if(side.state==='text')row.append(element('pre',{class:'history-preview-text',text:side.text||'(empty file)'}));
        }
      }
      changes.append(row);
    }
    if(this.changes?.next)changes.append(actionButton('Next changes',()=>{void this.loadChanges(this.changes.next);},{disabled:this.changes.busy||this.contextInvalid}));
    if(this.capturedGroup) {
      const group=this.capturedGroup;
      const panel=element('section',{'aria-label':'Recorded action group'},element('h4',{text:'Recorded action group'}),
        element('p',{text:'One recorded action; coverage is partial. Members are reauthorized before each comparison.'}));
      for(const member of group.members){
        const row=element('div',{class:'history-version-row'},element('strong',{text:member.current_name || member.current_resource_id}),element('span',{text:member.state}));
        if(member.state==='available' && group.locator && member.item?.after?.id && member.item?.after?.availability==='available' && !member.item.ambiguous_phase) {
          const memberID=member.current_resource_id, locator=group.locator, epoch=this.generation;
          row.append(actionButton('Open saved file',async()=>{
            const scope=window.chatModule?.ratingAccountScope?.(), sessions=window.sessionModule, navigation=sessions?.getSessionNavigationToken?.();
            const current=()=>{const now=window.chatModule?.ratingAccountScope?.();return this.guardContext(epoch) && this.capturedGroup===group && this.capturedActionLocator===locator && !!scope?.sid && !!scope?.owner && document.body.dataset.accountId===scope.owner && now?.sid===scope.sid && now?.owner===scope.owner && now?.authEpoch===scope.authEpoch && navigation!==undefined && sessions?.getCurrentSessionId?.()===scope.sid && sessions?.getSessionNavigationToken?.()===navigation;};
            try {if(!current())throw Error('Saved file unavailable.');const {openCapturedResult}=await import('./chatCapturedResource.js');
              if(current())await openCapturedResult({locator,chat:scope.sid,resourceID:memberID,isCurrent:current});
            } catch(_){if(current()){this.statusMessage='Saved file unavailable.';this.render();}}
          },{disabled:this.contextInvalid}));
        }
        if(member.state==='available')row.append(actionButton('Compare exact versions',()=>{void this.compareCapturedMember(member.current_resource_id);},{disabled:this.contextInvalid||this.changeComparison?.busy}));
        if(member.state==='available' && this.capturedActionLocator && this.target?.onRecordedImpact && member.item?.after?.id && member.item?.after?.version_id && !member.item.ambiguous_phase)row.append(actionButton('Analyze current impact',()=>{void this.analyzeCapturedImpact(member.current_resource_id,member.item);},{disabled:this.contextInvalid||Boolean(this.impactPending)}));
        const comparison=this.changeComparison;
        if(comparison?.action===group.actionID && comparison.resourceID===member.current_resource_id){
          if(comparison.busy)row.append(element('span',{text:'Reading versions…'}));
          else if(comparison.error)row.append(element('span',{text:comparison.error}));
          else for(const phase of ['before','after']){const side=comparison[phase];row.append(element('strong',{text:phase+': '+side.state}));if(side.state==='text')row.append(element('pre',{class:'history-preview-text',text:side.text||'(empty file)'}));}
        }
        panel.append(row);
      }
      changes.append(panel);
    }
    if (this.capturedActionLocator) {
      const page = this.observedStep, observedToken = this.generation;
      const observed = element('section', {'aria-label':'Observed step'}, element('h4',{text:'Observed step'}),
        element('p',{text:'Partial, unverified local observation; this does not establish complete step or effect coverage.'}));
      observed.append(actionButton(page?.busy ? 'Loading…' : 'Find observed steps',()=>{void this.loadObservedStep();},{disabled:page?.busy||this.contextInvalid}));
      if (page?.error) observed.append(element('p',{class:'history-state error',text:page.error}));
      if (page?.candidates && !page.error) {
        observed.append(element('p',{text:page.candidates.length ? 'Choose an observed step to inspect.' : 'No candidates in this partial observation.'}));
        page.candidates.forEach((candidate,index)=>observed.append(actionButton(`View step ${index+1} · ${candidate.coverage}`,()=>{void this.loadObservedStep({selector:candidate.selector});},{disabled:page.busy||this.contextInvalid})));
      }
      if (page?.members && !page.error) {
        for (const member of page.members) {
          const row = element('div',{class:'history-version-row'},element('span',{text:`${member.sequence} · ${member.state}`}));
          if (member.state === 'qualified') {
            row.append(element('span',{text:member.current_name || 'Recorded action group'}));
            const button = capturedChangeButton(member.locator,{isCurrent:()=>this.guardContext(observedToken),onResolved:data=>this.openCapturedAction(data,{expectedResource:member.current_resource_id || null,name:member.current_name || 'Recorded action group',locator:member.locator})});
            if (button) row.append(button);
          }
          observed.append(row);
        }
        if (page.next && !page.error) observed.append(actionButton('Next observed members',()=>{void this.loadObservedStep({selector:page.selector,cursor:page.next});},{disabled:page.busy||this.contextInvalid}));
      }
      changes.append(observed);
    }
    shell.append(changes);
    const managed=element('section',{class:'history-change-groups','aria-label':'Managed recorded change fragments'},
      element('h3',{text:'Managed recorded changes'}),element('p',{text:'This resource page selects actions by action ID. Groups follow their first action; actions within each group follow action ID. Groups may continue elsewhere; turn, shell and other-resource coverage are not complete.'}),
      actionButton(this.managedChanges?.busy?'Loading…':'Show managed changes',()=>{void this.loadManagedChanges();},{disabled:this.managedChanges?.busy||this.contextInvalid}));
    managed.append(actionButton('Recorded time',()=>{void this.loadOrderedChanges();},{disabled:this.managedChanges?.busy||this.contextInvalid}),element('p',{text:'Recorded-time pages omit actions without a timestamp; the amount omitted is unknown. An exact-origin page covers recorded actions on this resource only.'}));
    managed.append(actionButton('Root recorded activity',()=>{void this.loadOriginFragments(null,null,'root');},{disabled:this.originFragments?.busy||this.contextInvalid}));
    if(this.managedChanges?.error)managed.append(element('p',{class:'history-state error',text:this.managedChanges.error}));
    if(this.managedChanges&&!this.managedChanges.busy&&!this.managedChanges.error&&!this.managedChanges.groups.length)managed.append(element('p',{text:'No managed actions in this page.'}));
    const managedToken=this.generation;
    for(const group of this.managedChanges?.groups||[]){
      const label=this.managedChanges?.ordered ? (group.actions[0]?.origin ? `Actor ${group.actions[0].origin.actor_id || 'unknown'} · recorded action`:'Origin unknown · recorded action') : (group.group_id ? `Actor ${group.actor_id || 'unknown'} · group fragment`:'Origin unknown · ungrouped');
      const row=element('div',{class:'history-version-row'},element('strong',{text:label}));
      for(const member of group.actions){
        const change=member.change || {};
        row.append(element('span',{text:`${change.operation || 'Action'} · ${change.state || 'unknown'} · ${millisLabel(change.timestamp_millis)}`}));
        if(change.writer_identity)row.append(element('span',{text:`Authenticated principal ${change.actor_id || 'unknown'} · Native writer ${change.writer_identity.actor_id} · session ${change.writer_identity.session_id} · source ${change.writer_identity.source.actor_id}`}));
        const button=capturedChangeButton(member.locator,{isCurrent:()=>this.guardContext(managedToken),onResolved:data=>{this.openCapturedAction(data,{expectedResource:this.target.resourceId,name:this.target.name,locator:member.locator});}});
        if(button)row.append(button);else row.append(element('span',{text:'Exact comparison unavailable'}));
        if(button && this.changeSets){const set=this.changeSets,epoch=set.actionToken();const select=capturedChangeButton(member.locator,{isCurrent:()=>this.guardContext(managedToken),onResolved:()=>set.selectAction(member.locator,epoch)});if(select){select.textContent='Select for local set';row.append(select);}}
        if(member.origin && typeof change.action_id==='string')row.append(actionButton('Same recorded origin',()=>{void this.loadOrderedChanges(change.action_id);},{disabled:this.managedChanges?.busy||this.contextInvalid}));
        if(member.origin && typeof change.action_id==='string')row.append(actionButton('Across recorded origin',()=>{void this.loadOriginFragments(change.action_id);},{disabled:this.originFragments?.busy||this.contextInvalid}));
      }
      managed.append(row);
    }
    if(this.managedChanges?.next)managed.append(actionButton('Next managed actions',()=>{const page=this.managedChanges;void (page.ordered?this.loadOrderedChanges(page.selector,page.next):this.loadManagedChanges(page.next));},{disabled:this.managedChanges.busy||this.contextInvalid}));
    if (this.originFragments) {
      const page = this.originFragments;
      const fragments = element('section', {}, element('h4', {text:page.mode==='root'?'Root recorded activity':'Recorded origin across resources'}), element('p', {text:page.mode==='root'?'The selected file’s registered root and one History namespace only. Missing times, denied records and unobserved effects mean coverage is always partial; this is not all workspace history.':'Authorized recorded fragments only; this is not a complete turn or runtime timeline.'}));
      if (page.error) fragments.append(element('p', {class:'history-state error', text:page.error}));
      if (page.partial) fragments.append(element('p', {text:'Coverage is partial.'}));
      for (const item of page.items) {
        const row = element('div', {class:'history-version-row'}, element('strong', {text:item.name}), element('span', {text:`${item.state}${page.mode==='root'?'':` · ${item.availability}`} · ${item.recorded_millis == null ? 'Recorded time unknown' : millisLabel(item.recorded_millis)}`}));
        const button = capturedChangeButton(item.locator, {isCurrent:()=>this.guardContext(managedToken), onResolved:data=>{this.openCapturedAction(data,{expectedResource:item.resource_id,name:item.name,locator:item.locator});}});
        if (button) row.append(button); fragments.append(row);
        if(button && this.changeSets){const set=this.changeSets,epoch=set.actionToken();const select=capturedChangeButton(item.locator,{isCurrent:()=>this.guardContext(managedToken),onResolved:()=>set.selectAction(item.locator,epoch)});if(select){select.textContent='Select for local set';row.append(select);}}
      }
      if (page.next && !page.error) fragments.append(actionButton('Next fragments',()=>{void this.loadOriginFragments(page.selector,page.next,page.mode);},{disabled:page.busy||this.contextInvalid}));
      if (page.error) fragments.append(actionButton('Refresh recorded activity',()=>{void this.loadOriginFragments(page.selector,null,page.mode);},{disabled:page.busy||this.contextInvalid}));
      managed.append(fragments);
    }
    shell.append(managed);
    const repairAction = element('input', { type: 'text', placeholder: 'Recovery action ID from the saved receipt', 'aria-label': 'Recovery action ID' });
    repairAction.value = this.repairActionId || '';
    repairAction.maxLength = 256;
    repairAction.style.maxWidth = '100%';
    const repair = actionButton(this.repairBusy ? 'Repairing recovery copy…' : 'Repair recovery copy', async () => {
      const actionId = repairAction.value.trim();
      const token = this.generation;
      if (!actionId || this.repairBusy || !this.guardContext(token)) return;
      this.repairActionId = actionId;
      this.repairBusy = true;
      this.render();
      try {
        const response = await this.requestJson('/api/history/capture-repair/' + encodeURIComponent(actionId), { method: 'POST' });
        if (!this.guardContext(token)) return;
        this.statusMessage = response.message || 'Recovery copy repaired; saved operation was not repeated.';
        this.statusBad = false;
        void this.loadPage(this.pageCursors[this.pageIndex] || null);
      } catch (error) {
        if (!this.guardContext(token)) return;
        this.statusMessage = error.message || 'Recovery gap remains. The saved operation was not repeated.';
        this.statusBad = true;
      } finally {
        if (this.isCurrent(token)) { this.repairBusy = false; this.render(); }
      }
    }, { disabled: this.repairBusy || this.restoring || this.contextInvalid, focusKey: 'repair-capture' });
    shell.append(element('div', { class: 'history-browser-toolbar' }, repairAction, repair));
    if (this.pendingRestoreVerification?.resource_id === this.target?.resourceId) {
      shell.append(actionButton('Verify committed restore', async () => {
        const token = this.generation;
        if (!this.guardContext(token) || this.restoring) return;
        this.restoring = true;
        this.render();
        try {
          const response = await this.requestJson('/api/history/restore', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(this.pendingRestoreVerification),
          });
          if (!this.guardContext(token)) return;
          const proof = response?.Restore?.verification;
          if (response?.Restore?.outcome !== 'Complete') throw new Error('Committed restore verification is unavailable.');
          this.statusMessage = proof?.status === 'Verified' ? 'Committed restore verified against its selected source content.'
            : proof?.status === 'Mismatch' ? 'Restore committed; current content differs from its selected source.' : 'Restore committed; verification is still pending.';
          if (proof?.status !== 'Pending') this.pendingRestoreVerification = null;
          this.statusBad = proof?.status === 'Mismatch';
        } catch (error) {
          if (this.guardContext(token)) { this.statusMessage = 'Restore remains committed; verification is unavailable.'; this.statusBad = true; }
        } finally {
          if (this.isCurrent(token)) { this.restoring = false; this.render(); }
        }
      }, { disabled: this.restoring || this.contextInvalid, focusKey: 'verify-restore' }));
    }
    const layout = element('div', { class: 'history-browser-layout' });
    const listPanel = element('section', { class: 'history-version-list-panel', 'aria-label': 'Captured versions' });
    const list = element('div', { class: 'history-version-list' });
    if (this.listBusy && !this.items.length) {
      list.append(element('p', { class: 'history-state', text: 'Loading captured versions…' }));
    } else if (this.listError) {
      list.append(element('p', { class: 'history-state error', text: this.listError }));
      list.append(actionButton('Retry', () => { void this.loadPage(this.pageCursors[this.pageIndex] || null); }, {
        disabled: this.listBusy || this.contextInvalid,
        focusKey: 'retry-list',
      }));
    } else if (!this.items.length) {
      list.append(element('p', {
        class: 'history-state',
        text: this.pageIndex
          ? 'There are no more versions on this page. Older versions may have expired or been pruned by retention.'
          : 'No versions are currently available. History may not have captured this resource yet, or retention may have pruned older versions.',
      }));
    } else {
      this.items.forEach((version, index) => {
        const title = String(version.display_name || this.target.name);
        const row = element('button', {
          type: 'button',
          class: 'history-version-row' + (version.id === this.selectedRef ? ' selected' : ''),
          'aria-pressed': String(version.id === this.selectedRef),
          'data-history-focus': 'version-' + String(index),
          onclick: () => this.selectVersion(version, index),
        });
        row.disabled = this.restoring || this.contextInvalid;
        row.append(
          element('span', { class: 'history-version-name', text: title }),
          element('span', { class: 'history-version-time', text: millisLabel(version.timestamp_millis) }),
          element('span', { class: 'history-version-meta', text: descriptorLabel(version, this.target.name) }),
          element('span', { class: 'history-version-availability', text: this.versionAvailabilityLabel(version) }),
        );
        list.append(row);
      });
    }
    listPanel.append(element('h3', { text: 'Versions' }), list);
    const detailPanel = element('section', { class: 'history-version-detail', 'aria-label': 'Selected version' });
    detailPanel.append(element('h3', { text: 'Preview and restore' }));
    this.renderSelectedVersion(detailPanel);
    layout.append(listPanel, detailPanel);
    shell.append(layout);
    body.replaceChildren(shell);
    if (focused) {
      const nextFocus = [...body.querySelectorAll('[data-history-focus]')]
        .find((node) => node.getAttribute('data-history-focus') === focused);
      nextFocus?.focus({ preventScroll: true });
    }
  }

  versionAvailabilityLabel(version) {
    if (version?.availability === 'expired') return 'Pruned by retention';
    if (version?.availability === 'expiring') return 'Retention pending';
    if (version?.availability === 'absent') return 'Absent entry';
    if (version?.availability === 'available') return 'Available';
    return 'Unavailable';
  }

  renderSelectedVersion(panel) {
    const version = this.selectedVersion;
    const canAct = !this.contextInvalid && !this.restoring && !this.regularRestorePending && !this.checkpointPendingForAccount();
    if (!version) {
      panel.append(element('p', { class: 'history-state', text: 'Choose a version to inspect its content or review restoring it.' }));
      return;
    }
    panel.append(
      element('div', { class: 'history-selected-summary' },
        element('strong', { text: String(version.display_name || this.target.name) }),
        element('span', { text: millisLabel(version.timestamp_millis) }),
        element('span', { text: descriptorLabel(version, this.target.name) })),
    );
    const preview = element('section', { class: 'history-preview-panel', 'aria-label': 'Version content preview' });
    preview.append(element('h4', { text: 'Content preview' }));
    if (this.previewBusy) preview.append(element('p', { class: 'history-state', text: 'Loading a bounded preview…' }));
    else if (this.previewError) preview.append(element('p', { class: 'history-state error', text: this.previewError }));
    else {
      if (this.previewNote) preview.append(element('p', { class: 'history-state', text: this.previewNote }));
      if (this.previewText) preview.append(element('pre', { class: 'history-preview-text', text: this.previewText }));
      else if (!this.previewNote && version.can_read === true) preview.append(element('p', { class: 'history-state', text: 'No preview content is loaded.' }));
      else if (!this.previewNote) preview.append(element('p', { class: 'history-state', text: this.availabilityNote(version) }));
    }
    panel.append(preview);
    if (this.target.onSelectVersion) {
      const target = this.target, token = this.generation, selected = version.id;
      panel.append(actionButton('Use selected version', async () => {
        if (!this.guardContext(token) || this.target !== target || this.selectedRef !== selected
            || version.phase !== 'after' || version.availability !== 'available' || version.can_read !== true) return;
        try {
          await target.onSelectVersion({resource_id:target.resourceId, version_ref:selected});
        } catch (_) {
          if (this.guardContext(token) && this.target === target && this.selectedRef === selected) {
            this.statusMessage = 'Selected version could not be used.'; this.statusBad = true; this.render();
          }
        }
      }, {disabled:!canAct || version.phase !== 'after' || version.availability !== 'available' || version.can_read !== true,
          focusKey:'use-selected-version'}));
    }
    if (['available','absent'].includes(version.availability)) panel.append(actionButton(this.regularRestore?.busy?'Loading regular-file review…':'Review regular-file state',()=>{void this.reviewRegularRestore({version_ref:version.id});},{disabled:!canAct||this.regularRestore?.busy}));
    if (version.can_restore === true) {
      panel.append(actionButton(this.restorePreviewBusy ? 'Loading restore preview…' : 'Review restore', () => { void this.reviewRestore(); }, {
        disabled: !canAct || this.restorePreviewBusy,
        focusKey: 'review-restore',
      }));
    } else {
      panel.append(element('p', { class: 'history-state', text: 'This version cannot be restored.' }));
    }
    if (this.restorePreview) {
      const replacing = this.restorePreview.effect === 'replace';
      const review = element('section', { class: 'history-restore-review', 'aria-label': 'Restore confirmation' });
      review.append(element('h4', { text: 'Review restore' }));
      review.append(element('p', {
        text: replacing
          ? 'This will replace the current file with the selected historical version.'
          : 'This will create the file from the selected historical version.',
      }));
      review.append(element('p', { text: 'The current destination will be captured in History before restoration.' }));
      review.append(element('p', { text: 'Confirm only if this effect matches what you want.' }));
      review.append(
        actionButton('Confirm restore', () => { void this.confirmRestore(); }, {
          disabled: !canAct || this.restoring,
          focusKey: 'confirm-restore',
        }),
        actionButton('Cancel review', () => { this.restorePreview = null; this.render(); }, {
          disabled: this.restoring,
          focusKey: 'cancel-review',
        }),
      );
      panel.append(review);
    }
  }
}

let view = null;

export function openFolderHistory(input, trigger = null) {
  if (!view) view = new ResourceHistoryView();
  return view.openFolder(input, trigger);
}

export function openResourceHistory(target, trigger = null) {
  if (!view) view = new ResourceHistoryView();
  return view.open(target, trigger);
}

/** Reuse the resource History window for one explicitly selected checkpoint. */
export function openCheckpointComparison(target, {requestId, load}, trigger = null) {
  if (!view) view = new ResourceHistoryView();
  if (typeof load!=='function' || typeof requestId!=='string' || !view.open(target,trigger))return false;
  void view.compareCheckpoint(requestId,load);
  return true;
}

export function openCheckpointRestore(target, {checkpointID, client}, trigger = null) {
  if (!view) view = new ResourceHistoryView();
  if (!client || typeof checkpointID!=='string' || !view.open(target,trigger))return false;
  void view.previewCheckpointRestore(checkpointID,client);
  return true;
}

function isCapturedLocator(locator) {
  return typeof locator==='string' && locator.length<=4096 && (locator.startsWith('oc-history-action:v1:') || locator.startsWith('oc-history-action:v2:'));
}
function validObservedGroupMembers(members) {
  return Array.isArray(members) && members.length>=2 && members.length<=64 && members.every(member=>member &&
    ['available','unavailable','pruned'].includes(member.state) && /^resource-[0-9a-f]{64}$/.test(member.current_resource_id||'') &&
    (member.current_name===undefined || (typeof member.current_name==='string' && member.current_name.length<=1024)));
}
function validCapturedGroup(data) {
  return data?.version===2 && data.coverage==='partial_recorded_action' && typeof data.action_id==='string' && data.action_id.length>0 && data.action_id.length<=512 &&
    validObservedGroupMembers(data.members) && new Set(data.members.map(x=>x.current_resource_id)).size===data.members.length &&
    data.members.every(member=>typeof member.resource_id==='string' && member.resource_id.length<=512 &&
      (member.state==='available' ? member.item && member.item.action_id===data.action_id : !('item' in member)));
}

/** Resolve only through the current account and the server's reauthorized public identity. */
export async function resolveCapturedChange(locator, isCurrent, allowFallback = true) {
  if(!isCapturedLocator(locator))throw Error('Captured change unavailable.');
  const liveScope=window.chatModule?.ratingAccountScope?.();
  const scope=liveScope && {owner:liveScope.owner,sid:liveScope.sid,authEpoch:liveScope.authEpoch};
  const account=document.body.dataset.accountId;
  const current=()=>{const now=window.chatModule?.ratingAccountScope?.();if(!account||document.body.dataset.accountId!==account||(typeof isCurrent==='function'&&isCurrent()!==true))return false;
    if(scope?.owner===account)return !!now&&scope.owner===now.owner&&scope.authEpoch===now.authEpoch&&scope.sid===now.sid;
    return allowFallback && typeof isCurrent==='function';};
  if(!current())throw Error('Captured change unavailable.');
  const response=await fetch('/api/history/captured-change',{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({locator})});
  if(!current()||!response.ok)throw Error('Captured change unavailable.');
  const data=await response.json();
  if(!current()||!(validCapturedGroup(data)||(data?.version===1&&data.coverage==='one_recorded_action'&&typeof data.resource_id==='string'&&/^resource-[0-9a-f]{64}$/.test(data.current_resource_id||'')&&data.item&&typeof data.item.action_id==='string')))throw Error('Captured change unavailable.');
  return data;
}

/** Same-root recorded changes only; no claim of complete turn coverage. */
export function capturedTurnButton(locator) {
  if(!isCapturedLocator(locator))return null;
  const box=element('span',{}), button=element('button',{type:'button',class:'copal-btn',text:'View recorded turn changes'});
  const scope=window.chatModule?.ratingAccountScope?.(), account=document.body.dataset.accountId;
  const current=()=>{const now=window.chatModule?.ratingAccountScope?.();return box.isConnected && !!account && document.body.dataset.accountId===account && !!scope && !!now && scope.owner===account && scope.owner===now.owner && scope.sid===now.sid && scope.authEpoch===now.authEpoch;};
  box.append(button);button.addEventListener('click',async event=>{
    event.stopPropagation();if(button.disabled || !current())return;button.disabled=true;
    try {
      const response=await fetch('/api/history/observed-step',{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:1,locator,turn:true})});
      const data=await response.json();if(!current() || !response.ok || data?.version!==1 || data.coverage!=='partial_recorded_actions' || !Array.isArray(data.actions) || !data.actions.length || data.actions.length>64)throw Error('Recorded changes unavailable');
      const details=element('details',{}), title=element('summary',{text:'Recorded changes (partial coverage)'});details.append(title);
      if(data.unknown)details.append(element('p',{text:'Some changes are unavailable or not recorded.'}));
      for(const action of data.actions){
        if(action?.state!=='available'){details.append(element('p',{text:'Recorded action unavailable.'}));continue;}
        const value=action.comparison;
        if(!(validCapturedGroup(value)||(value?.version===1&&value.coverage==='one_recorded_action'&&value.item&&/^resource-[0-9a-f]{64}$/.test(value.current_resource_id||''))))throw Error('Recorded changes unavailable');
        const resource=value.version===2?value.members[0].current_resource_id:value.current_resource_id;
        const open=element('button',{type:'button',class:'copal-btn',text:'Open recorded action'});
        open.addEventListener('click',event=>{event.stopPropagation();if(!current())return;if(!view)view=new ResourceHistoryView();if(view.open({resourceId:resource,name:'Recorded action',isContextCurrent:current,exactOnly:true},open))view.openCapturedAction(value,{locator:action.locator});});details.append(open);
      }
      if(current()){button.replaceWith(details);details.open=true;}
    } catch {if(current()){button.textContent='Recorded changes unavailable';button.disabled=false;}}
  });return box;
}

/** A locator selects an action; current host account and resource permissions authorize it. */
export function capturedChangeButton(locator, {isCurrent,onResolved} = {}) {
  if(!isCapturedLocator(locator))return null;
  const button=element('button',{type:'button',class:'copal-btn',text:'View captured change'});
  button.addEventListener('click',async event=>{
    event.stopPropagation();if(button.disabled)return;button.disabled=true;
    const current=()=>button.isConnected&&(typeof isCurrent!=='function'||isCurrent()===true);
    try {
      const data=await resolveCapturedChange(locator,current,typeof isCurrent==='function');if(!current())return;
      if(typeof onResolved==='function'){onResolved(data);return;}
      if(!view)view=new ResourceHistoryView();
      if(!view.open({resourceId:data.version===2?data.members[0].current_resource_id:data.current_resource_id,name:'Captured change',isContextCurrent:current,exactOnly:true},button))return;
      view.openCapturedAction(data,{locator});
    } catch(error) {if(current()){button.title=String(error?.message||'Captured change is unavailable.');button.textContent='Captured change unavailable';}}
    finally {if(button.isConnected)button.disabled=false;}
  });
  return button;
}
