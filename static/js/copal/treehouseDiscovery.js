// One scoped, body-free discovery response shared by the Skills and Library views.
export const rootOrder=['treehouse:programming','treehouse:games','treehouse:ai','treehouse:openclank'];
export const list=value=>Array.isArray(value)?value:Object.values(value||{});
export function searchShared(catalogue,query='',rootId='') {
 const q=String(query).trim().toLocaleLowerCase(),quests=list(catalogue?.quests).filter(x=>!rootId||x.mainQuestId===rootId),ids=new Set(quests.map(x=>x.id));
 const matches=x=>!q||[x.title,x.objective,x.outcome].some(v=>String(v||'').toLocaleLowerCase().includes(q));
 return {quests:quests.filter(matches),lessons:list(catalogue?.lessons).filter(x=>ids.has(x.courseId)&&matches(x))};
}
export function questConnections(catalogue,questId) {
 const lessons=new Map(list(catalogue?.lessons).map(x=>[x.id,x]));
 return list(catalogue?.topicLinks).filter(x=>lessons.get(x.fromId)?.courseId===questId&&lessons.has(x.toId)).map(x=>({...x,from:lessons.get(x.fromId),to:lessons.get(x.toId)}));
}
export function createTreeHouseDiscovery(context) {
 let cached=null,pending=null,version=0;
 const scope=()=>{const s=context().ui.snapshot;return JSON.stringify([s?.accountId,s?.workspace,s?.globalCatalogue?.sourceId,s?.globalCatalogue?.packageRevision,s?.globalCatalogue?.packageDigest,s?.globalCatalogue?.progressFingerprint,s?.globalAvailability?.status]);};
 const invalidate=()=>{version++;cached=null;pending=null;};
 async function load({fresh=false}={}) {
  const key=scope();if(fresh)invalidate();
  if(cached?.key===key)return cached.data;if(pending?.key===key)return pending.promise;
  const ticket=version;
  const promise=(async()=>{
   const data=await context().api('/treehouse/learning-library');
   if(ticket!==version||scope()!==key)return null;
   const snapshot=context().ui.snapshot;
   if(data.accountId!==snapshot.accountId||data.workspace!==snapshot.workspace)throw Error('Learning access changed. Reload this view.');
   const shared=data.sharedCatalogue;
   if(shared&&(shared.sourceId!=='openclank:treehouse-core'||shared.packageRevision!==snapshot.globalCatalogue?.packageRevision||(snapshot.globalCatalogue?.packageDigest&&shared.packageDigest!==snapshot.globalCatalogue.packageDigest)||(snapshot.globalCatalogue?.progressFingerprint&&shared.progressFingerprint!==snapshot.globalCatalogue.progressFingerprint)))throw Error('Curriculum changed. Reload TreeHouse.');
   cached={key,data,packageDigest:shared?.packageDigest,progressFingerprint:shared?.progressFingerprint};return data;
  })();pending={key,promise};
  try{return await promise;}finally{if(pending?.promise===promise)pending=null;}
 }
 return {load,invalidate};
}
