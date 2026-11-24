import {createWindowNavigation} from './navigation.js';
// Explicit presentation fields only; never put answer text or curriculum bodies in history.
export function treeHouseLocation(value={}) {
 const result={};
 for(const key of ['section','mode','selectedCourse','privateItem','selectedSkill','recordCourse','skillDisplay','skillsRoot','skillsQuest','libraryQuery','libraryRoot'])if(typeof value[key]==='string')result[key]=value[key].slice(0,key==='libraryQuery'?240:256);
 if(Number.isFinite(value.scroll))result.scroll=Math.max(0,value.scroll);
 if(value.topic)result.topic={rootId:value.topic.rootId||null,courseId:value.topic.courseId||null,unitId:value.topic.unitId||null,privateMode:!!value.topic.privateMode};
 if(value.source)result.source={kind:value.source.kind,sourceId:value.source.sourceId||null};
 return result;
}
export function createTreeHouseNavigation({getScope,capture,restore,onChange=()=>{}}) {
 let key='',disposed=false;const positions=new Map();
 const locationKey=entry=>JSON.stringify({...entry,scroll:0});
 function checkpoint(){const entry=treeHouseLocation(capture());positions.set(locationKey(entry),entry.scroll||0);while(positions.size>100)positions.delete(positions.keys().next().value);}
 const model=createWindowNavigation({maxEntries:100,restore:async(entry,info)=>{
  const origin=key;
  const current=()=>!disposed&&origin===scopeKey()&&model.generation===info.generation;
  if(!current())return false;
  const location=treeHouseLocation(entry);location.scroll=positions.get(locationKey(location))??location.scroll;
  return (await restore(location,current))===true&&current();
 },onChange});
 const scopeKey=()=>JSON.stringify(getScope()||null);
 function sync(){const next=scopeKey();if(next!==key){key=next;positions.clear();model.setScope(getScope());}return next;}
 function record(){sync();if(disposed)return false;return model.commit(treeHouseLocation(capture()));}
 function ensure(){sync();if(!model.snapshot().length)record();}
 async function move(direction){sync();const pending=model[direction]();onChange(model.snapshot());try{return await pending;}finally{onChange(model.snapshot());}}
 return {record,ensure,checkpoint,reset(){key='';positions.clear();model.reset();},snapshot:()=>model.snapshot(),back:()=>move('back'),forward:()=>move('forward'),canGoBack:()=>model.canGoBack(),canGoForward:()=>model.canGoForward(),dispose(){disposed=true;model.dispose();onChange(model.snapshot());}};
}
