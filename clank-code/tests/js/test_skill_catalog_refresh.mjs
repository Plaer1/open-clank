import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import vm from 'node:vm';
async function mounted() {
  let sid='s1', failed=false, cold=false, gate;
  const calls=[], events=[], errors=[], toasts=[];
  const nodes=new Map([['new-skill-name',{value:'sample'}],['new-skill-description',{value:'Sample skill'}],['skill-import-url',{value:'https://example.test/skill'}]]);
  const doc={getElementById:id=>nodes.get(id)||null,querySelectorAll:()=>[],querySelector:()=>null,addEventListener(){},dispatchEvent:event=>events.push(event.type)};
  const fetcher=async(url,options={})=>{
    calls.push({url,options});
    if(url.includes('mimo-operation')&&gate){const wait=gate;gate=null;await wait;}
    const mutation=options.method && url.includes('/api/skills/');
    return {ok:!(mutation&&failed)&&!(url.includes('mimo-operation')&&cold),status:409,json:async()=>url.endsWith('/api/skills')?{skills:[{name:'sample',status:'draft',revision:1,content_hash:'h'}]}:url.includes('/import-from-url')?{skill:{name:'sample'}}:url.endsWith('/status')?{status:'idle'}:{}};
  };
  const context=vm.createContext({window:{location:{origin:'http://host'},fetch:fetcher},document:doc,fetch:fetcher,CustomEvent:class{constructor(type){this.type=type;}},console,encodeURIComponent,setTimeout,clearInterval,localStorage:{getItem(){return null;}},CSS:{escape:value=>value}});
  const modules=new Map();
  async function load(name){
    if(modules.has(name))return modules.get(name);
    let module;
    if(['skills.js','nativeCatalogRefresh.js'].includes(name)){
      let code=await fs.readFile(new URL('../../static/js/'+name,import.meta.url),'utf8');
      if(name==='skills.js')code+='\nexport {addSkill,importSkillFromUrl,_setSkillStatus,_deleteSkill,_saveSkillEdit,_saveBuiltinEdit,_revertBuiltin,_bulkApprove,_bulkDelete,_bulkDeleteNonPassing}; export function selectForTest(){_selectedNames.clear();_selectedNames.add("sample");}\n';
      module=new vm.SourceTextModule(code,{context,identifier:name});
    }else if(name==='storage.js')module=new vm.SyntheticModule(['get','KEYS'],function(){this.setExport('get',()=>sid);this.setExport('KEYS',{CURRENT_SESSION:'currentSessionId'});},{context});
    else module=new vm.SyntheticModule(['default','uiIcon','bindMenuDismiss','dismissOrRemove','topPortalZ'],function(){this.setExport('default',{esc:String,showToast:value=>toasts.push(value),showError:value=>errors.push(value),styledConfirm:async()=>true});this.setExport('uiIcon',()=>'<svg/>');for(const key of ['bindMenuDismiss','dismissOrRemove','topPortalZ'])this.setExport(key,()=>0);},{context});
    modules.set(name,module);if(module instanceof vm.SourceTextModule)await module.link(spec=>load(spec.split('/').at(-1)));return module;
  }
  const skills=await load('skills.js');await skills.evaluate(); const refresh=await load('nativeCatalogRefresh.js');
  return {skills:skills.namespace,refresh:refresh.namespace.refreshReadyNativeCommands,calls,events,errors,toasts,nodes,setSid:value=>sid=value,setFailed:value=>failed=value,setCold:value=>cold=value,pauseRefresh(){let release;gate=new Promise(resolve=>release=resolve);return release;}};
}
test('mounted shipped add/edit/delete/publish/import/builtin callbacks refresh only after successful writes',async()=>{
  const app=await mounted();const ui=app.skills;
  const editor={value:'updated markdown'};const card={querySelector:()=>({querySelector:()=>editor})};
  await ui.addSkill();await ui._saveSkillEdit(card,'sample');await ui._setSkillStatus('sample','published');await ui._setSkillStatus('sample','draft');await ui._deleteSkill('sample');await ui.importSkillFromUrl();
  await ui._saveBuiltinEdit({querySelector:()=>editor},'sample');await ui._revertBuiltin('sample');
  const mutations=app.calls.filter(call=>call.options.method&&call.url.includes('/api/skills/'));
  const refresh=app.calls.filter(call=>call.url.includes('mimo-operation'));
  assert.equal(refresh.length,mutations.length);assert.equal(refresh.length,8);
  for(const call of refresh){assert.equal(call.url,'/api/session/s1/mimo-operation');assert.deepEqual(JSON.parse(call.options.body),{operation:'commands-refresh',reload_skills:true});}
  assert.equal(app.events.filter(event=>event==='odysseus:skills-changed').length,8);
  assert.equal(app.calls.some(call=>call.url.includes('mimo-state')||call.url.includes('refresh=true')),false);
});
test('failed shipped writes never refresh or report deletion success; successful bulk callbacks refresh',async()=>{
  const app=await mounted();app.setFailed(true);
  await app.skills.addSkill();await app.skills._setSkillStatus('sample','published');await app.skills._deleteSkill('sample');
  assert.equal(app.events.length,0);assert.equal(app.calls.some(call=>call.url.includes('mimo-operation')),false);assert.ok(app.errors.some(error=>error.startsWith('Delete failed')));
  app.setFailed(false);await app.skills.loadSkills();app.skills.selectForTest();await app.skills._bulkApprove();app.skills.selectForTest();await app.skills._bulkDelete();
  assert.equal(app.calls.filter(call=>call.url.includes('mimo-operation')).length,2);
});
test('ready-only helper invalidates immediately, keeps cold/no-session targets cold and fences completion to original chat',async()=>{
  const app=await mounted();app.setSid('');await app.refresh();assert.equal(app.calls.length,0);assert.deepEqual(app.events,['odysseus:command-catalog-changed']);
  app.setSid('s1');app.setCold(true);await app.refresh({reloadSkills:false,changedEvent:'odysseus:mcp-changed'});assert.equal(app.events.at(-1),'odysseus:mcp-changed');assert.deepEqual(JSON.parse(app.calls[0].options.body),{operation:'commands-refresh',reload_skills:false});
  app.setCold(false);const release=app.pauseRefresh();const pending=app.refresh({reloadSkills:true,changedEvent:'odysseus:skills-changed'});app.setSid('s2');release();const result=await pending;assert.equal(result.reason,'chat-changed');assert.equal(app.events.at(-1),'odysseus:skills-changed');
});
