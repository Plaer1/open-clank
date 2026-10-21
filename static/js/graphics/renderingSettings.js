import {graphicalPresentationPolicy} from './presentationPolicy.js';
export function mountRenderingSettings(root,{policy=graphicalPresentationPolicy()}={}){
  if(!root||root.dataset.renderingMounted)return;
  root.dataset.renderingMounted='1';
  const input=root.querySelector('[data-graphical-rendering-mode]'),status=root.querySelector('[data-graphical-rendering-status]');
  const paint=()=>{input.value=policy.mode;status.textContent=policy.mode==='ordinary'
    ? 'Ordinary images and software map rendering. Browser/OS compositing may still use a GPU. Text editing remains DOM.'
    : 'Automatic eligible graphical surfaces. Actual backend depends on capability, visibility and resource limits; hardware is not identified. Text editing remains DOM.';};
  input.addEventListener('change',()=>{policy.set(input.value);paint();});
  const graph=document.createElement('button');graph.type='button';graph.className='copal-btn';graph.textContent='Graph settings…';graph.addEventListener('click',()=>window.dispatchEvent(new CustomEvent('openclank:graph-settings')));root.append(graph);
  policy.subscribe(paint);paint();
}
