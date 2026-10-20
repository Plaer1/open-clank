import {layoutCodeMap} from './codeMapLayout.js';
// Persistent dedicated worker: one bounded job executes at a time. Parent owns
// coalescing and rejects obsolete namespace/serial results before any painting.
self.onmessage=event=>{
  const input=event.data;
  try{self.postMessage(layoutCodeMap(input));}
  catch{self.postMessage({serial:input?.serial,namespace:input?.namespace,error:'invalid_layout'});}
};
