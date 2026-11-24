import { getNativeSettingsReference, searchNativeSettingsReference } from './nativeSettingsReference.js';
import { nativeSettingControls } from './nativeAgentSettings.js';

let mounted = null;
const translate = text => window.openClankI18n?.t?.(text) || text;
const options = () => ({controls:nativeSettingControls(),translate});
export function nativeReferenceSearchTerms() {
  return getNativeSettingsReference(options()).flatMap(row=>row.searchTerms).join(' ');
}
export function nativeReferenceCatalogEntries() {
  // The reference itself is always readable; its underlying capability may be
  // unavailable. Never give a read-only row the identity of a working writer.
  return getNativeSettingsReference(options()).filter(row=>row.disposition !== 'configured-control').map(row=>({
    identity:row.identity, token:'settings ai reference:' + row.path, aliases:[],
    help:row.description + ' · ' + row.disposition + (row.reason ? ' · ' + row.reason : ''),
    category:'Native settings reference', origin:'host', executor:'settings', scope:row.scope,
    availability:'available', destination:{panel:'ai',field:'reference:' + row.path},
    underlyingAvailability:row.availability, writable:false,
  }));
}
export function mountNativeSettingsReference({card,openSettings}) {
  if (!card || mounted?.card === card) return;
  const section=document.createElement('details'); section.className='admin-card';
  section.dataset.nativeSettingsReference='';
  const heading=document.createElement('summary'); heading.textContent=translate('Native settings reference');
  const intro=document.createElement('p'); intro.className='admin-toggle-sub';
  intro.textContent=translate('Search native configuration paths and their Open Clank destinations. Reference entries do not change configuration.');
  const input=document.createElement('input'); input.type='search'; input.className='settings-select';
  input.placeholder=translate('Search paths, features or destinations'); input.setAttribute('aria-label',input.placeholder);
  const status=document.createElement('p'); status.setAttribute('role','status');
  const list=document.createElement('div');
  const more=document.createElement('button'); more.type='button'; more.className='btn-secondary'; more.textContent=translate('Show more');
  section.append(heading,intro,input,status,list,more); card.after(section);
  let limit=40;
  function render() {
    const rows=searchNativeSettingsReference(input.value,options()); list.replaceChildren();
    status.textContent=rows.length + ' ' + translate('matching paths') + ' · ' + Math.min(limit,rows.length) + ' ' + translate('shown');
    rows.slice(0,limit).forEach(row=>{
      const item=document.createElement('details'); item.dataset.nativeReferencePath=row.path;
      const title=document.createElement('summary'); title.textContent=row.path + ' — ' + row.description;
      const explanation=document.createElement('p'); explanation.className='admin-toggle-sub';
      explanation.textContent=[row.category,row.disposition,row.scope,row.reason].filter(Boolean).join(' · ');
      item.append(title,explanation);
      if(row.destination?.panel) {
        const button=document.createElement('button'); button.type='button'; button.className='btn-secondary';
        const supported=row.disposition==='configured-control';
        button.textContent=translate(supported ? 'Open control' : 'Open related settings');
        button.onclick=()=>openSettings(row.destination.panel,supported ? {field:row.destination.field} : {});
        item.append(button);
      }
      list.append(item);
    });
    more.hidden=rows.length <= limit;
  }
  input.addEventListener('input',()=>{limit=40;render();});
  more.onclick=()=>{limit+=40;render();};
  document.addEventListener('openclank:settings-catalog-changed',render);
  render();
  mounted={card,section,input,list,render};
}
export function focusNativeSettingsReference(path) {
  if(!mounted)return false;
  mounted.section.open=true; mounted.input.value=path; mounted.render();
  const row=[...mounted.list.children].find(item=>item.dataset.nativeReferencePath.toLowerCase()===path.toLowerCase());
  if(!row)return false;
  row.open=true; row.querySelector('summary').focus(); row.scrollIntoView({block:'nearest'}); return true;
}
